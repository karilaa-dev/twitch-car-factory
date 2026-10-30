from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock

import pytest

from controller.twitch_miner.client import AuthenticationError, TwitchError
from controller.twitch_miner.engine import DropsMiner

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def campaign():
    return {
        "id": "campaign", "name": "Campaign", "game": {"id": "game"}, "status": "ACTIVE",
        "startAt": (NOW - timedelta(hours=1)).isoformat(),
        "endAt": (NOW + timedelta(hours=1)).isoformat(),
        "self": {"isAccountConnected": True}, "allow": {"channels": []},
        "timeBasedDrops": [{
            "id": "drop", "name": "Reward", "requiredMinutesWatched": 30,
            "benefitEdges": [{"benefit": {"id": "benefit"}}], "preconditionDrops": [],
            "self": {"currentMinutesWatched": 0, "isClaimed": False, "dropInstanceID": None},
        }],
    }


class FakeClient:
    user_id = "user"

    def __init__(self):
        self.campaign = campaign()
        self.inventory = []
        self.calls = []
        self.live = {"first": "other-game", "second": "game"}
        self.watch = Mock(return_value=True)
        self.reset_playback = Mock()
        self.claim_points = Mock(return_value=True)
        self.send_points_watch = Mock(return_value=True)
        self.progress = None
        self.claim_status = "ELIGIBLE_FOR_ALL"
        self.available = {"campaign"}

    def stream(self, login):
        return {"id": login, "stream": {"id": "stream"},
                "broadcastSettings": {"game": {"id": self.live[login]}}} if login in self.live else None

    def available_drop_campaigns(self, channel_id):
        return [deepcopy(self.campaign)] if self.campaign["id"] in self.available else []

    def gql(self, operation, variables=None):
        self.calls.append((operation, variables))
        if operation == "Inventory":
            return {"currentUser": {"inventory": {"dropCampaignsInProgress": deepcopy(self.inventory), "gameEventDrops": []}}}
        if operation == "Campaigns":
            return {"currentUser": {"dropCampaigns": [deepcopy(self.campaign)]}}
        if operation == "CampaignDetails":
            return {"user": {"dropCampaign": deepcopy(self.campaign)}}
        if operation == "CurrentDrop":
            current = deepcopy(self.progress)
            if current is not None and "channel" not in current:
                current["channel"] = {"id": variables["channelID"]}
            return {"currentUser": {"dropCurrentSession": current}}
        if operation == "ClaimDrop":
            return {"claimDropRewards": {"status": self.claim_status}}
        raise AssertionError(operation)


def setup():
    client = FakeClient()
    emit = Mock()
    miner = DropsMiner(client, "alice", ("first", "second", "offline"), emit, threading.Event())
    return client, miner, emit


def test_discovers_unstarted_campaign_and_prefers_eligible_configured_channel():
    client, miner, emit = setup()
    miner.step(100, NOW)
    client.watch.assert_called_once_with("second")
    client.claim_points.assert_called_once_with("second")
    emit.assert_called_with("watching_channels", channels=["second"], online_channels=["first", "second"])
    assert ("CampaignDetails", {"channelLogin": "user", "dropID": "campaign"}) in client.calls
    client.send_points_watch.assert_called_once_with("second", "second", "stream", "game", "")
    assert not any(op == "ClaimDrop" for op, _ in client.calls)
    # Watch every heartbeat, but do not refetch the whole inventory each time.
    client.calls.clear()
    miner.step(120, NOW)
    assert client.watch.call_count == 2
    assert client.calls == []


def test_confirmed_completion_claims_synthesized_id_and_unlocks_next_drop():
    client, miner, _ = setup()
    second = deepcopy(client.campaign["timeBasedDrops"][0])
    second.update(id="second-drop", preconditionDrops=[{"id": "drop"}])
    client.campaign["timeBasedDrops"].append(second)
    client.progress = {"dropID": "drop", "currentMinutesWatched": 30}
    miner.step(100, NOW)
    assert ("ClaimDrop", {"input": {"dropInstanceID": "user#campaign#drop"}}) in client.calls
    assert miner.campaigns["campaign"].drops["drop"].is_claimed
    assert miner.campaigns["campaign"].earnable_drops(NOW)[0].id == "second-drop"
    # Lagging inventory must not undo a confirmed claim.
    miner.step(160, NOW)
    assert len([op for op, _ in client.calls if op == "ClaimDrop"]) == 1


def test_inventory_claims_on_start_and_failed_claims_retry_without_marking_success():
    client, miner, _ = setup()
    completed = deepcopy(client.campaign)
    completed["timeBasedDrops"][0]["self"].update(currentMinutesWatched=30, dropInstanceID="server-claim")
    client.inventory = [completed]
    client.claim_status = "NOT_ELIGIBLE"
    miner.step(100, NOW)
    assert not miner.campaigns["campaign"].drops["drop"].is_claimed
    assert ("ClaimDrop", {"input": {"dropInstanceID": "server-claim"}}) in client.calls
    client.claim_status = "DROP_INSTANCE_ALREADY_CLAIMED"
    miner.step(160, NOW)
    assert miner.campaigns["campaign"].drops["drop"].is_claimed


@pytest.mark.parametrize("change", ["unlinked", "completed", "disabled", "expired", "allowlist"])
def test_no_eligible_drops_falls_back_to_points_in_source_order(change):
    client, miner, _ = setup()
    if change == "unlinked":
        client.campaign["self"]["isAccountConnected"] = False
    elif change == "completed":
        client.campaign["timeBasedDrops"][0]["self"]["isClaimed"] = True
    elif change == "disabled":
        client.available = set()
    elif change == "allowlist":
        client.campaign["allow"] = {"isEnabled": True, "channels": [{"id": "unconfigured"}]}
    else:
        client.campaign["endAt"] = NOW.isoformat()
    miner.step(100, NOW)
    client.watch.assert_called_once_with("first")


def test_playback_failure_reports_no_watch_and_rotates():
    client, miner, emit = setup()
    client.live["first"] = "game"
    client.watch.side_effect = [False, True]
    miner.step(100, NOW)
    emit.assert_called_with("watching_channels", channels=[], online_channels=["first", "second"])
    miner.step(120, NOW)
    assert [call.args[0] for call in client.watch.call_args_list] == ["first", "second"]


def test_offline_switch_and_channel_game_change_trigger_discovery():
    client, miner, emit = setup()
    miner.step(100, NOW)
    del client.live["second"]
    miner.step(160, NOW)
    emit.assert_called_with("watching_channels", channels=["first"], online_channels=["first"])
    assert sum(op == "Campaigns" for op, _ in client.calls) == 2


def test_stall_rotates_but_does_not_invent_progress():
    client, miner, _ = setup()
    client.live["first"] = "game"
    for now in range(100, 461, 60):
        miner.step(now, NOW)
    assert client.watch.call_args.args == ("second",)
    assert miner.campaigns["campaign"].drops["drop"].current_minutes == 0
    assert not any(op == "ClaimDrop" for op, _ in client.calls)


def test_progress_for_unrelated_drop_never_claims():
    client, miner, _ = setup()
    client.progress = {"dropID": "unknown-drop", "currentMinutesWatched": 999}
    miner.step(100, NOW)
    assert not any(op == "ClaimDrop" for op, _ in client.calls)


def test_transient_points_error_does_not_block_drop_claim():
    client, miner, _ = setup()
    client.claim_points.side_effect = TwitchError("temporary")
    client.progress = {"dropID": "drop", "currentMinutesWatched": 30}
    miner.step(100, NOW)
    assert miner.campaigns["campaign"].drops["drop"].is_claimed


def test_run_stops_on_authentication_failure_and_clears_telemetry():
    client, miner, emit = setup()
    client.stream = Mock(side_effect=AuthenticationError("rejected"))
    with pytest.raises(AuthenticationError):
        miner.run()
    emit.assert_called_with("watching_channels", channels=[], online_channels=[])


def test_cancelled_miner_does_not_make_requests():
    client, miner, _ = setup()
    miner.stopped.set()
    miner.run()
    assert not client.calls


def test_dashboard_fields_survive_partial_campaign_details():
    client, miner, _ = setup()
    original = client.gql

    def gql(operation, variables=None):
        result = original(operation, variables)
        if operation == "CampaignDetails":
            detail = result["user"]["dropCampaign"]
            for field in ("status", "game", "self"):
                detail.pop(field)
        return result

    client.gql = gql
    miner.step(100, NOW)
    client.watch.assert_called_once_with("second")


def test_points_heartbeat_follows_playback_but_bonus_claims_remain_minutely():
    client, miner, _ = setup()
    miner.step(100, NOW)
    miner.next_refresh = 0
    miner.step(120, NOW)
    assert client.send_points_watch.call_count == 2
    assert client.claim_points.call_count == 1
    miner.step(160, NOW)
    assert client.send_points_watch.call_count == 3
    assert client.claim_points.call_count == 2


def test_transient_claim_error_does_not_prevent_playback():
    client, miner, _ = setup()
    completed = deepcopy(client.campaign)
    completed["timeBasedDrops"][0]["self"].update(currentMinutesWatched=30, dropInstanceID="claim")
    client.inventory = [completed]
    original = client.gql

    def gql(operation, variables=None):
        if operation == "ClaimDrop":
            raise TwitchError("temporary")
        return original(operation, variables)

    client.gql = gql
    miner.step(100, NOW)
    client.watch.assert_called_once()
    client.send_points_watch.assert_called_once()
    assert not miner.claimed


def test_single_stalled_channel_refreshes_playback_without_idle_cooldown():
    client, miner, _ = setup()
    del client.live["first"]
    for now in range(100, 461, 60):
        miner.step(now, NOW)
    client.reset_playback.assert_called_once_with("second")
    assert client.watch.call_count == 7
    assert not miner.cooldowns


def test_failed_campaign_detail_retains_previous_data_and_retries_soon():
    client, miner, _ = setup()
    miner.step(100, NOW)
    original = client.gql

    def gql(operation, variables=None):
        if operation == "CampaignDetails":
            raise TwitchError("temporary")
        return original(operation, variables)

    client.gql = gql
    miner.next_discovery = 0
    miner.step(160, NOW)
    assert miner.next_discovery == 220
    assert "campaign" in miner.campaigns
    assert client.watch.call_args.args == ("second",)


def configure_tv_discovery(client):
    discovered = deepcopy(client.campaign)
    for key in ("startAt", "status", "self", "allow"):
        discovered.pop(key, None)
    for drop in discovered["timeBasedDrops"]:
        drop["startAt"] = client.campaign["startAt"]
        drop["endAt"] = client.campaign["endAt"]
        drop.pop("self", None)
        drop.pop("preconditionDrops", None)
    client.available_drop_campaigns = lambda channel_id: [deepcopy(discovered)] if channel_id == "second" else []
    original = client.gql

    def gql(operation, variables=None):
        if operation == "Campaigns":
            return {"currentUser": {"dropCampaigns": []}}
        if operation == "CampaignDetails":
            return {"user": {"dropCampaign": None}}
        return original(operation, variables)

    client.gql = gql
    return discovered


def test_tv_empty_dashboard_trials_advertised_channel_then_uses_inventory_claim():
    client, miner, _ = setup()
    configure_tv_discovery(client)
    miner.step(100, NOW)
    client.watch.assert_called_once_with("second")
    assert miner.campaigns["campaign"].linked is None
    assert miner.campaigns["campaign"].drops["drop"].current_minutes == 0
    assert not miner.claimed
    completed = deepcopy(client.campaign)
    completed["timeBasedDrops"][0]["self"].update(currentMinutesWatched=30, dropInstanceID="actual-claim")
    client.inventory = [completed]
    miner.step(160, NOW)
    assert miner.campaigns["campaign"].linked is True
    assert ("ClaimDrop", {"input": {"dropInstanceID": "actual-claim"}}) in client.calls
    assert miner.campaigns["campaign"].drops["drop"].is_claimed


def test_tv_inventory_unlinked_state_overrides_unknown_channel_discovery():
    client, miner, _ = setup()
    configure_tv_discovery(client)
    miner.step(100, NOW)
    client.inventory = [deepcopy(client.campaign)]
    client.inventory[0]["self"]["isAccountConnected"] = False
    miner.step(160, NOW)
    assert miner.campaigns["campaign"].linked is False
    assert client.watch.call_args.args == ("first",)


@pytest.mark.parametrize("minutes,subscriptions", [(0, 1), (30, 1)])
def test_tv_subscription_rewards_never_become_timed_drop_candidates(minutes, subscriptions):
    client, miner, _ = setup()
    discovered = configure_tv_discovery(client)
    discovered["timeBasedDrops"][0].update(requiredMinutesWatched=minutes, requiredSubs=subscriptions)
    miner.step(100, NOW)
    client.watch.assert_called_once_with("first")
    assert not miner.claimed


def test_tv_inventory_progress_proves_exact_channel_even_without_advertised_campaign():
    client, miner, _ = setup()
    client.available = set()
    client.inventory = [deepcopy(client.campaign)]
    client.progress = {"channel": {"id": "second"}, "dropID": "drop", "currentMinutesWatched": 1}
    miner.step(100, NOW)
    client.watch.assert_called_once_with("second")
    assert miner.campaigns["campaign"].drops["drop"].current_minutes == 1
    assert miner.online[0].confirmed_campaigns == set()
    assert miner.online[1].confirmed_campaigns == {"campaign"}
    client.progress["currentMinutesWatched"] = 30
    miner.step(160, NOW)
    assert ("ClaimDrop", {"input": {"dropInstanceID": "user#campaign#drop"}}) in client.calls


def test_current_session_for_another_channel_never_proves_eligibility_or_progress():
    client, miner, _ = setup()
    client.available = set()
    client.inventory = [deepcopy(client.campaign)]
    client.progress = {"channel": {"id": "not-configured"}, "dropID": "drop", "currentMinutesWatched": 30}
    miner.step(100, NOW)
    client.watch.assert_called_once_with("first")
    assert not any(channel.confirmed_campaigns for channel in miner.online)
    assert miner.campaigns["campaign"].drops["drop"].current_minutes == 0
    assert not miner.claimed


@pytest.mark.parametrize("session_response", ["missing", "failed", "stale"])
def test_increasing_inventory_progress_prevents_false_stall(session_response):
    client, miner, _ = setup()
    client.live["first"] = "game"
    client.inventory = [deepcopy(client.campaign)]
    original = client.gql

    def gql(operation, variables=None):
        if operation == "CurrentDrop" and session_response == "failed":
            raise TwitchError("Current session unavailable")
        return original(operation, variables)

    client.gql = gql
    if session_response == "stale":
        client.progress = {"dropID": "drop", "currentMinutesWatched": 0}
    for minute in range(7):
        client.inventory[0]["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = minute + 1
        miner.step(100 + minute * 60, NOW)
    assert all(call.args == ("first",) for call in client.watch.call_args_list)
    client.reset_playback.assert_not_called()
    assert not miner.cooldowns
    assert miner.campaigns["campaign"].drops["drop"].current_minutes == 7


@pytest.mark.parametrize("operation", ["Inventory", "Campaigns"])
@pytest.mark.parametrize("cached", [False, True])
def test_metadata_outage_preserves_playback_and_points_until_recovery(operation, cached):
    client, miner, _ = setup()
    if cached:
        miner.step(100, NOW)
    original = client.gql
    attempts = []

    def gql(name, variables=None):
        if name == operation:
            attempts.append(name)
            raise TwitchError("Metadata unavailable")
        return original(name, variables)

    client.gql = gql
    miner.next_discovery = 0
    for now in range(160, 521, 20):
        miner.step(now, NOW)
    assert len(attempts) == 7  # Metadata retries are bounded to once per minute.
    assert client.watch.call_count == 19 + int(cached)
    assert client.send_points_watch.call_count == client.watch.call_count
    assert client.claim_points.call_count == 7 + int(cached)
    assert client.watch.call_args.args == (("second",) if cached else ("first",))
    client.gql = original
    miner.step(580, NOW)
    assert client.watch.call_args.args == ("second",)


@pytest.mark.parametrize("operation", ["Inventory", "Campaigns"])
def test_metadata_authentication_failure_still_stops_collection(operation):
    client, miner, _ = setup()
    original = client.gql

    def gql(name, variables=None):
        if name == operation:
            raise AuthenticationError("Session rejected")
        return original(name, variables)

    client.gql = gql
    with pytest.raises(AuthenticationError):
        miner.step(100, NOW)
    client.watch.assert_not_called()
