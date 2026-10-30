"""Pure models for Twitch's campaign inventory and timed drop eligibility.

The protocol follows DevilXD/TwitchDropsMiner; attribution is recorded in
THIRD_PARTY_NOTICES.md. Progress always comes from Twitch, never elapsed time.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


SPECIAL_GAME_IDS = frozenset({"509663", "509672"})


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Campaign timestamp is missing or invalid.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Campaign timestamp is missing or invalid.") from None
    if stamp.tzinfo is None:
        raise ValueError("Campaign timestamp must include a timezone.")
    return stamp.astimezone(timezone.utc)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Campaign identifier is missing or invalid.")
    return value


def _minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Drop watch minutes must be a nonnegative integer.")
    return value


def merge_campaign(inventory: dict, details: dict) -> dict:
    """Combine campaign shapes without losing progress or unstarted drops.

    Inventory is authoritative when both responses contain a value. Timed
    drops are merged by ID rather than replacing the complete details list.
    Neither input is modified.
    """

    merged = deepcopy(details)
    for key, value in inventory.items():
        if key == "timeBasedDrops":
            by_id = {drop["id"]: deepcopy(drop) for drop in details.get(key) or []}
            ordered = []
            for drop in value or []:
                identifier = drop["id"]
                ordered.append(merge_campaign(drop, by_id.pop(identifier, {})))
            merged[key] = ordered + list(by_id.values())
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_campaign(value, merged[key])
        elif value is not None or key not in merged:
            merged[key] = deepcopy(value)
    return merged


def normalize_discovered_campaign(data: dict) -> dict:
    """Fill window metadata absent from the channel discovery response.

    Never fabricate link state, progress, or claim state. The engine supplies
    the exact advertising channels separately so unknown link state can only
    permit a trial there. Inventory/details take precedence over these fields.
    """
    normalized = deepcopy(data)
    drops = normalized.get("timeBasedDrops") or []
    if not normalized.get("startAt"):
        starts = [_timestamp(drop.get("startAt")) for drop in drops]
        if not starts:
            raise ValueError("Discovered campaign contains no timed drops.")
        normalized["startAt"] = min(starts).isoformat()
    if not normalized.get("status"):
        # Start/end windows still govern actual availability, including future
        # drops. This response advertises campaigns currently served by Twitch.
        normalized["status"] = "ACTIVE"
    return normalized


def claimed_benefits_from_inventory(values: list[dict] | None) -> dict[str, datetime]:
    """Read award timestamps used when Twitch omits a drop's self edge."""

    result = {}
    for item in values or []:
        try:
            result[_identifier(item.get("id"))] = _timestamp(item.get("lastAwardedAt"))
        except (AttributeError, ValueError):
            continue
    return result


@dataclass(slots=True)
class Drop:
    id: str
    name: str
    current_minutes: int
    required_minutes: int
    required_subscriptions: int
    claim_id: str | None
    is_claimed: bool
    starts_at: datetime
    ends_at: datetime
    prerequisite_ids: tuple[str, ...]
    benefit_ids: tuple[str, ...]
    campaign: Campaign = field(repr=False)

    def prerequisites_met(self) -> bool:
        return all(
            identifier in self.campaign.drops
            and self.campaign.drops[identifier].is_claimed
            for identifier in self.prerequisite_ids
        )

    def can_earn(self, now: datetime) -> bool:
        is_prerequisite = any(
            self.id in drop.prerequisite_ids for drop in self.campaign.drops.values()
        )
        return (
            not self.is_claimed
            and self.required_minutes > 0
            and self.required_subscriptions == 0
            and self.current_minutes < self.required_minutes
            and self.starts_at <= now < self.ends_at
            and self.prerequisites_met()
            and (bool(self.benefit_ids) or is_prerequisite)
        )

    def can_claim(self, now: datetime) -> bool:
        # Instance IDs can be known before completion. A generated claim ID
        # must never be mistaken for proof of watched minutes.
        return (
            not self.is_claimed
            and self.required_minutes > 0
            and self.required_subscriptions == 0
            and self.current_minutes >= self.required_minutes
            and self.starts_at <= now
            and now < self.campaign.ends_at + timedelta(hours=24)
            and self.prerequisites_met()
        )


@dataclass(slots=True)
class Campaign:
    id: str
    name: str
    game_id: str
    linked: bool | None
    starts_at: datetime
    ends_at: datetime
    status: str
    allowed_channel_ids: frozenset[str]
    has_badge_or_emote: bool
    advertised_channel_ids: frozenset[str] = frozenset()
    drops: dict[str, Drop] = field(default_factory=dict)

    @classmethod
    def from_data(
        cls, data: dict, claimed_benefits: dict[str, datetime] | None = None,
        *, advertised_channel_ids: frozenset[str] = frozenset(),
    ) -> Campaign:
        claimed_benefits = claimed_benefits or {}
        game = data.get("game")
        if not isinstance(game, dict):
            raise ValueError("Campaign game is missing or invalid.")
        allowed = data.get("allow") or {}
        channels = allowed.get("channels") or []
        starts_at = _timestamp(data.get("startAt"))
        ends_at = _timestamp(data.get("endAt"))
        if ends_at <= starts_at:
            raise ValueError("Campaign end must follow its start.")
        link_state = (data.get("self") or {}).get("isAccountConnected")
        if link_state is not None and not isinstance(link_state, bool):
            raise ValueError("Campaign account-link state is invalid.")
        campaign = cls(
            id=_identifier(data.get("id")),
            name=str(data.get("name") or ""),
            game_id=_identifier(game.get("id")),
            linked=link_state,
            starts_at=starts_at,
            ends_at=ends_at,
            status=str(data.get("status") or ""),
            allowed_channel_ids=frozenset(
                _identifier(channel.get("id")) for channel in channels
            ) if allowed.get("isEnabled", True) else frozenset(),
            has_badge_or_emote=False,
            advertised_channel_ids=advertised_channel_ids,
        )
        for item in data.get("timeBasedDrops") or []:
            identifier = _identifier(item.get("id"))
            if identifier in campaign.drops:
                raise ValueError("Campaign contains duplicate drops.")
            benefits = [edge["benefit"] for edge in item.get("benefitEdges") or []]
            benefit_ids = tuple(_identifier(benefit.get("id")) for benefit in benefits)
            campaign.has_badge_or_emote |= any(
                benefit.get("distributionType") in {"BADGE", "EMOTE"}
                for benefit in benefits
            )
            self_edge = item.get("self") or {}
            required = _minutes(item.get("requiredMinutesWatched"))
            current_value = self_edge.get("currentMinutesWatched")
            current = _minutes(0 if current_value is None else current_value)
            start = _timestamp(item.get("startAt") or data.get("startAt"))
            end = _timestamp(item.get("endAt") or data.get("endAt"))
            if end <= start:
                raise ValueError("Drop end must follow its start.")
            claimed = self_edge.get("isClaimed") is True
            # Require every reward to have been awarded in this drop's window;
            # overlapping rewards in another campaign are insufficient evidence.
            if not self_edge and benefit_ids:
                claimed = all(
                    identifier in claimed_benefits
                    and start <= claimed_benefits[identifier] < end
                    for identifier in benefit_ids
                )
            claim_id = self_edge.get("dropInstanceID")
            if claim_id is not None:
                claim_id = _identifier(claim_id)
            campaign.drops[identifier] = Drop(
                id=identifier,
                name=str(item.get("name") or ""),
                current_minutes=required if claimed else min(current, required),
                required_minutes=required,
                required_subscriptions=_minutes(item.get("requiredSubs") or 0),
                claim_id=claim_id,
                is_claimed=claimed,
                starts_at=start,
                ends_at=end,
                prerequisite_ids=tuple(
                    _identifier(prerequisite.get("id"))
                    for prerequisite in item.get("preconditionDrops") or []
                ),
                benefit_ids=benefit_ids,
                campaign=campaign,
            )
        return campaign

    def earnable_drops(self, now: datetime, *, allow_unknown_link: bool = False) -> list[Drop]:
        if (
            self.status not in ("ACTIVE", "UPCOMING")
            or not self.starts_at <= now < self.ends_at
            or not (
                self.linked is True or self.has_badge_or_emote
                or (self.linked is None and allow_unknown_link)
            )
        ):
            return []
        return [drop for drop in self.drops.values() if drop.can_earn(now)]

    def eligible_for(self, channel_id: str, game_id: str, now: datetime) -> bool:
        return (
            (game_id == self.game_id or self.game_id in SPECIAL_GAME_IDS)
            and (not self.allowed_channel_ids or channel_id in self.allowed_channel_ids)
            and bool(self.earnable_drops(
                now, allow_unknown_link=channel_id in self.advertised_channel_ids,
            ))
        )

    def claimable_drops(self, now: datetime) -> list[Drop]:
        # Expired campaigns and disconnected accounts can still contain earned
        # drops. Claiming is independent of eligibility to earn new progress.
        return [drop for drop in self.drops.values() if drop.can_claim(now)]
