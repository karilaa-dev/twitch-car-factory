from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from controller.twitch_miner.inventory import (
    Campaign,
    claimed_benefits_from_inventory,
    merge_campaign,
)


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def drop(identifier="drop", **updates):
    data = {
        "id": identifier,
        "name": identifier,
        "startAt": (NOW - timedelta(hours=1)).isoformat(),
        "endAt": (NOW + timedelta(hours=1)).isoformat(),
        "requiredMinutesWatched": 30,
        "preconditionDrops": [],
        "benefitEdges": [{"benefit": {"id": "reward-" + identifier, "distributionType": "DIRECT_ENTITLEMENT"}}],
        "self": {"currentMinutesWatched": 0, "isClaimed": False, "dropInstanceID": None},
    }
    data.update(updates)
    return data


def campaign(**updates):
    data = {
        "id": "campaign",
        "name": "Campaign",
        "game": {"id": "game"},
        "startAt": (NOW - timedelta(hours=1)).isoformat(),
        "endAt": (NOW + timedelta(hours=1)).isoformat(),
        "status": "ACTIVE",
        "self": {"isAccountConnected": True},
        "allow": {"channels": [], "isEnabled": False},
        "timeBasedDrops": [drop()],
    }
    data.update(updates)
    return data


def test_eligible_campaign_checks_game_link_and_windows():
    data = campaign()
    model = Campaign.from_data(data)
    assert model.eligible_for("channel", "game", NOW)
    assert not model.eligible_for("channel", "different", NOW)
    assert not model.eligible_for("channel", "game", NOW + timedelta(hours=2))
    assert not model.eligible_for("channel", "game", NOW - timedelta(hours=2))
    data["self"]["isAccountConnected"] = False
    assert not Campaign.from_data(data).eligible_for("channel", "game", NOW)


@pytest.mark.parametrize("status", ["EXPIRED", "UNKNOWN"])
def test_nonactive_campaigns_cannot_earn(status):
    assert not Campaign.from_data(campaign(status=status)).earnable_drops(NOW)


def test_upcoming_campaign_becomes_earnable_at_start_without_refresh():
    model = Campaign.from_data(campaign(
        status="UPCOMING", startAt=(NOW + timedelta(minutes=1)).isoformat(),
    ))
    assert not model.earnable_drops(NOW)
    assert model.eligible_for("channel", "game", NOW + timedelta(minutes=1))


def test_allowed_channel_ids_apply_only_when_enabled():
    data = campaign(allow={"channels": [{"id": "allowed"}], "isEnabled": True})
    model = Campaign.from_data(data)
    assert model.eligible_for("allowed", "game", NOW)
    assert not model.eligible_for("other", "game", NOW)
    data["allow"]["isEnabled"] = False
    assert Campaign.from_data(data).eligible_for("other", "game", NOW)


@pytest.mark.parametrize("game_id", ["509663", "509672"])
def test_special_game_campaign_can_earn_on_any_game(game_id):
    model = Campaign.from_data(campaign(game={"id": game_id}))
    assert model.eligible_for("channel", "different", NOW)


def test_badge_campaign_does_not_need_game_account_connection():
    data = campaign(self={"isAccountConnected": False})
    data["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["distributionType"] = "BADGE"
    assert Campaign.from_data(data).eligible_for("channel", "game", NOW)


def test_prerequisite_claim_unlocks_next_drop_and_missing_prerequisite_blocks():
    model = Campaign.from_data(campaign(timeBasedDrops=[
        drop("first"),
        drop("second", preconditionDrops=[{"id": "first"}]),
        drop("third", preconditionDrops=[{"id": "missing"}]),
    ]))
    assert [item.id for item in model.earnable_drops(NOW)] == ["first"]
    model.drops["first"].is_claimed = True
    assert [item.id for item in model.earnable_drops(NOW)] == ["second"]


def test_drop_specific_window_and_rewardless_prerequisite():
    model = Campaign.from_data(campaign(timeBasedDrops=[
        drop("first", benefitEdges=[]),
        drop("second", preconditionDrops=[{"id": "first"}]),
        drop("future", startAt=(NOW + timedelta(minutes=10)).isoformat()),
        drop("expired", endAt=(NOW - timedelta(minutes=10)).isoformat()),
        drop("empty", benefitEdges=[]),
    ]))
    assert [item.id for item in model.earnable_drops(NOW)] == ["first"]


def test_instance_id_does_not_prove_completion_and_complete_drop_can_generate_id():
    data = campaign()
    data["timeBasedDrops"][0]["self"]["dropInstanceID"] = "known-before-completion"
    model = Campaign.from_data(data)
    assert not model.claimable_drops(NOW)
    model.drops["drop"].current_minutes = 30
    assert model.claimable_drops(NOW) == [model.drops["drop"]]
    model.drops["drop"].claim_id = None
    assert model.drops["drop"].can_claim(NOW)


def test_completed_drop_claims_within_campaign_grace_without_link():
    data = campaign(status="EXPIRED", self={"isAccountConnected": False})
    data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = 30
    model = Campaign.from_data(data)
    assert model.claimable_drops(NOW + timedelta(hours=24))
    assert not model.claimable_drops(NOW + timedelta(hours=25))
    model.drops["drop"].is_claimed = True
    assert not model.claimable_drops(NOW)


def test_inventory_merge_preserves_progress_and_all_detailed_drops_without_mutation():
    detail = campaign(timeBasedDrops=[drop("one"), drop("two")])
    inventory = {"id": "campaign", "timeBasedDrops": [
        {"id": "one", "self": {"currentMinutesWatched": 18}},
    ]}
    before = deepcopy((inventory, detail))
    merged = merge_campaign(inventory, detail)
    model = Campaign.from_data(merged)
    assert set(model.drops) == {"one", "two"}
    assert model.drops["one"].current_minutes == 18
    assert model.drops["one"].required_minutes == 30
    assert model.drops["two"].current_minutes == 0
    assert (inventory, detail) == before


def test_claimed_benefit_fallback_requires_awards_in_drop_window_and_all_rewards():
    data = campaign()
    del data["timeBasedDrops"][0]["self"]
    benefits = claimed_benefits_from_inventory([
        {"id": "reward-drop", "lastAwardedAt": NOW.isoformat()},
        {"id": "bad", "lastAwardedAt": "invalid"},
    ])
    assert Campaign.from_data(data, benefits).drops["drop"].is_claimed
    assert not Campaign.from_data(data, {"reward-drop": NOW - timedelta(days=1)}).drops["drop"].is_claimed
    data["timeBasedDrops"][0]["benefitEdges"].append({"benefit": {"id": "another"}})
    assert not Campaign.from_data(data, benefits).drops["drop"].is_claimed


def test_authoritative_unclaimed_self_takes_precedence_over_shared_benefit():
    model = Campaign.from_data(campaign(), {"reward-drop": NOW})
    assert not model.drops["drop"].is_claimed


@pytest.mark.parametrize("minutes", [-1, True, False, float("nan"), float("inf"), 1.5, "30"])
def test_invalid_watch_progress_is_rejected(minutes):
    data = campaign()
    data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = minutes
    with pytest.raises(ValueError, match="minutes"):
        Campaign.from_data(data)


def test_watch_progress_is_bounded_and_claimed_drop_is_complete():
    data = campaign()
    data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = 1000
    assert Campaign.from_data(data).drops["drop"].current_minutes == 30
    data["timeBasedDrops"][0]["self"].update(currentMinutesWatched=0, isClaimed=True)
    assert Campaign.from_data(data).drops["drop"].current_minutes == 30


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-10-30T00:00:00"])
def test_invalid_or_naive_timestamps_are_rejected(timestamp):
    with pytest.raises(ValueError, match="timestamp"):
        Campaign.from_data(campaign(startAt=timestamp))



def test_unknown_link_state_only_allows_trial_on_advertising_channel():
    data = campaign()
    data.pop("self")
    model = Campaign.from_data(data, advertised_channel_ids=frozenset({"verified-channel"}))
    assert model.linked is None
    assert model.eligible_for("verified-channel", "game", NOW)
    assert not model.eligible_for("unverified-channel", "game", NOW)
    assert not model.earnable_drops(NOW)
    model.linked = False
    assert not model.eligible_for("verified-channel", "game", NOW)


def test_subscriptions_are_not_earned_or_claimed_as_watched_minutes():
    data = campaign()
    data["timeBasedDrops"][0]["requiredSubs"] = 1
    data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = 30
    model = Campaign.from_data(data)
    assert not model.earnable_drops(NOW)
    assert not model.claimable_drops(NOW)
