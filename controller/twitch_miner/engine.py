"""Polling headless adaptation of TwitchDropsMiner's collection flow.

Only the immutable configured channels are candidates. One playback session per
account avoids competing drop progress; no progress is invented from elapsed time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
import time

from .client import AuthenticationError, TwitchClient, TwitchError
from .inventory import Campaign, claimed_benefits_from_inventory, merge_campaign, normalize_discovered_campaign

logger = logging.getLogger(__name__)
WATCH_INTERVAL = 20
REFRESH_INTERVAL = 60
DISCOVERY_INTERVAL = 600
STALL_INTERVAL = 300


@dataclass
class Channel:
    login: str
    id: str
    game_id: str
    campaigns: set[str]
    broadcast_id: str = ""
    game_name: str = ""
    discovered_campaigns: dict[str, dict] = field(default_factory=dict)
    confirmed_campaigns: set[str] = field(default_factory=set)
    current_drop: dict | None = None


class DropsMiner:
    def __init__(self, client: TwitchClient, username: str, channels: tuple[str, ...],
                 emit, stopped: threading.Event):
        self.client = client
        self.username = username
        self.channels = channels
        self.emit = emit
        self.stopped = stopped
        self.online: list[Channel] = []
        self.campaigns: dict[str, Campaign] = {}
        self.details: dict[str, dict] = {}
        self.claimed: set[tuple[str, str]] = set()
        self.cooldowns: dict[str, float] = {}
        self.next_refresh = 0.0
        self.next_discovery = 0.0
        self.next_points: dict[str, float] = {}
        self.watching: Channel | None = None
        self.progress: tuple[str, int] | None = None
        self.progress_at = 0.0

    def refresh_channels(self) -> None:
        online = []
        for login in self.channels:
            if self.stopped.is_set():
                return
            user = self.client.stream(login)
            if not user or not user.get("stream"):
                continue
            game = (user.get("broadcastSettings") or {}).get("game") or {}
            channel = Channel(login, str(user["id"]), str(game.get("id", "")), set(),
                              str(user["stream"]["id"]), str(game.get("name", "")))
            try:
                channel.discovered_campaigns = {
                    str(item["id"]): item
                    for item in self.client.available_drop_campaigns(channel.id)
                }
                channel.campaigns = set(channel.discovered_campaigns)
            except AuthenticationError:
                raise
            except TwitchError:
                # A stream without verified campaign eligibility can still earn points.
                logger.warning("Could not verify drops eligibility for %s.", login)
            online.append(channel)
        self.online = online

    def refresh_inventory(self, now: float) -> None:
        data = self.client.gql("Inventory")
        inventory = (data.get("currentUser") or {}).get("inventory")
        if not isinstance(inventory, dict):
            raise TwitchError("Twitch inventory is unavailable.")
        ongoing = {str(c["id"]): c for c in inventory.get("dropCampaignsInProgress") or []}
        if now >= self.next_discovery:
            dashboard = self.client.gql("Campaigns")
            user = dashboard.get("currentUser")
            if not isinstance(user, dict):
                raise TwitchError("Twitch campaigns are unavailable.")
            relevant_games = {c.game_id for c in self.online} | {"509663", "509672"}
            advertised = set().union(*(c.campaigns for c in self.online))
            details = {}
            retry_details = False
            for campaign in user.get("dropCampaigns") or []:
                if self.stopped.is_set():
                    return
                cid = str(campaign["id"])
                if campaign.get("status") not in ("ACTIVE", "UPCOMING"):
                    continue
                if (str((campaign.get("game") or {}).get("id")) not in relevant_games
                        and cid not in advertised and cid not in ongoing):
                    continue
                try:
                    response = self.client.gql("CampaignDetails", {
                        "channelLogin": str(self.client.user_id), "dropID": cid,
                    })
                except AuthenticationError:
                    raise
                except TwitchError:
                    retry_details = True
                    logger.warning("Campaign detail request failed; will retry.")
                    if cid in self.details:
                        details[cid] = self.details[cid]
                    continue
                detail = (response.get("user") or {}).get("dropCampaign")
                if isinstance(detail, dict):
                    details[cid] = merge_campaign(campaign, detail)
                else:
                    details[cid] = campaign
            self.details = details
            self.next_discovery = now + (REFRESH_INTERVAL if retry_details else DISCOVERY_INTERVAL)
        discovered = {}
        advertised_channels: dict[str, set[str]] = {}
        for channel in self.online:
            for cid, data in channel.discovered_campaigns.items():
                advertised_channels.setdefault(cid, set()).add(channel.id)
                try:
                    discovered[cid] = normalize_discovered_campaign(data)
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
        benefits = claimed_benefits_from_inventory(inventory.get("gameEventDrops") or [])
        campaigns = {}
        for cid in self.details.keys() | ongoing.keys() | discovered.keys():
            try:
                detail = merge_campaign(self.details.get(cid, {}), discovered.get(cid, {}))
                merged = merge_campaign(ongoing.get(cid, {}), detail)
                campaign = Campaign.from_data(
                    merged, benefits,
                    advertised_channel_ids=frozenset(advertised_channels.get(cid, ())),
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                logger.warning("Skipping a malformed Twitch campaign.")
                continue
            for drop in campaign.drops.values():
                if (cid, drop.id) in self.claimed:
                    drop.is_claimed = True
            campaigns[cid] = campaign
        self.campaigns = campaigns

    def refresh_current_sessions(self) -> None:
        """Twitch's exact-channel progress can prove eligibility absent discovery.

        TV tokens can omit timed campaigns from AvailableDrops while Inventory
        and CurrentDrop still report real accrual. Twitch can also return a
        session for another stream, so both channel and drop IDs must match.
        """
        for channel in self.online:
            if self.stopped.is_set():
                return
            try:
                data = self.client.gql("CurrentDrop", {"channelID": channel.id})
            except AuthenticationError:
                raise
            except TwitchError:
                logger.warning("Current drop session check failed; will retry.")
                continue
            current = (data.get("currentUser") or {}).get("dropCurrentSession")
            if not isinstance(current, dict):
                continue
            current_channel = current.get("channel") or {}
            if not isinstance(current_channel, dict) or str(current_channel.get("id", "")) != channel.id:
                continue
            minutes = current.get("currentMinutesWatched")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
                continue
            for campaign in self.campaigns.values():
                if str(current.get("dropID", "")) in campaign.drops:
                    channel.current_drop = current
                    channel.confirmed_campaigns.add(campaign.id)
                    break

    def claim_drops(self, wall_now: datetime) -> None:
        for campaign in self.campaigns.values():
            for drop in campaign.claimable_drops(wall_now):
                if self.stopped.is_set():
                    return
                claim_id = drop.claim_id or f"{self.client.user_id}#{campaign.id}#{drop.id}"
                try:
                    response = self.client.gql("ClaimDrop", {"input": {"dropInstanceID": claim_id}})
                except AuthenticationError:
                    raise
                except TwitchError:
                    logger.warning("Drop claim request failed; will retry.")
                    continue
                result = response.get("claimDropRewards") or {}
                if not response.get("errors") and result.get("status") in ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"):
                    drop.is_claimed = True
                    self.claimed.add((campaign.id, drop.id))
                    logger.info("Claimed Twitch drop: %s", drop.name)
                else:
                    logger.warning("Twitch has not confirmed the drop claim; will retry.")

    def eligible(self, channel: Channel, wall_now: datetime) -> bool:
        return any(c.id in (channel.campaigns | channel.confirmed_campaigns) and c.eligible_for(channel.id, channel.game_id, wall_now)
                   for c in self.campaigns.values())

    def update_progress(self, channel: Channel, now: float, wall_now: datetime) -> None:
        current = channel.current_drop
        if current is None:
            return
        for campaign in self.campaigns.values():
            drop = campaign.drops.get(str(current.get("dropID", "")))
            if (drop is None or campaign.id not in (channel.campaigns | channel.confirmed_campaigns)
                    or not campaign.eligible_for(channel.id, channel.game_id, wall_now)):
                continue
            minutes = current.get("currentMinutesWatched")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
                return
            drop.current_minutes = min(minutes, drop.required_minutes)
            progress = (drop.id, drop.current_minutes)
            if progress != self.progress:
                self.progress, self.progress_at = progress, now
                logger.info("Drop progress: %s (%s/%s minutes)", drop.name,
                            drop.current_minutes, drop.required_minutes)
            return

    def report(self, watched: Channel | None) -> None:
        self.emit("watching_channels", channels=[watched.login] if watched else [],
                  online_channels=[c.login for c in self.online])

    def step(self, now: float, wall_now: datetime) -> None:
        refresh = now >= self.next_refresh
        if refresh:
            previous_games = {c.game_id for c in self.online}
            self.refresh_channels()
            if {c.game_id for c in self.online} != previous_games:
                self.next_discovery = 0
            self.refresh_inventory(now)
            self.refresh_current_sessions()
            self.claim_drops(wall_now)
            self.next_refresh = now + REFRESH_INTERVAL
        if self.stopped.is_set():
            return
        candidates = [c for c in self.online if self.cooldowns.get(c.login, 0) <= now]
        eligible = [c for c in candidates if self.eligible(c, wall_now)]
        channel = next(iter(eligible or candidates), None)
        if channel is None:
            self.watching = None
            self.report(None)
            return
        if self.watching is None or channel.login != self.watching.login:
            self.progress, self.progress_at = None, now
            logger.info("Watching %s for %s.", channel.login, "drops and points" if eligible else "points")
        self.watching = channel
        if not self.client.watch(channel.login):
            self.cooldowns[channel.login] = now + REFRESH_INTERVAL
            self.watching = None
            self.next_refresh = 0
            self.report(None)
            logger.warning("Playback unavailable; selecting another configured channel.")
            return
        self.report(channel)
        # Match the previous points miner's playback cadence. Bonus claims
        # are polled separately; neither path may block working drop playback.
        try:
            if not self.client.send_points_watch(
                channel.login, channel.id, channel.broadcast_id, channel.game_id, channel.game_name
            ):
                logger.warning("Channel-points heartbeat was not accepted; will retry.")
            if now >= self.next_points.get(channel.login, 0):
                self.next_points[channel.login] = now + REFRESH_INTERVAL
                self.client.claim_points(channel.login)
        except AuthenticationError:
            raise
        except TwitchError:
            logger.warning("Channel-points check failed; will retry.")
        if refresh:
            if eligible:
                self.update_progress(channel, now, wall_now)
                self.claim_drops(wall_now)
                if now - self.progress_at >= STALL_INTERVAL:
                    self.client.reset_playback(channel.login)
                    if len(eligible) > 1:
                        self.cooldowns[channel.login] = now + STALL_INTERVAL
                    self.watching = None
                    logger.warning("No confirmed drop progress; rotating configured channels.")

    def run(self) -> None:
        failures = 0
        try:
            while not self.stopped.is_set():
                started = time.monotonic()
                try:
                    self.step(started, datetime.now(timezone.utc))
                    failures = 0
                except AuthenticationError:
                    raise
                except TwitchError:
                    failures += 1
                    self.report(None)
                    logger.warning("Twitch request failed; retrying (%s/5).", failures)
                    if failures >= 5:
                        raise TwitchError("Twitch remained unavailable after five attempts.") from None
                self.stopped.wait(max(1, WATCH_INTERVAL - (time.monotonic() - started)))
        finally:
            self.emit("watching_channels", channels=[], online_channels=[])
