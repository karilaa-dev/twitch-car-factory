"""Small, fail-open Twitch identity lookup boundary for channel editors."""

from __future__ import annotations

import copy
from enum import StrEnum
import json
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from TwitchChannelPointsMiner.constants import CLIENT_ID, GQLOperations


TWITCH_LOOKUP_TIMEOUT_SECONDS = 5
TWITCH_LOOKUP_BATCH_SIZE = 50


class TwitchLookupStatus(StrEnum):
    EXISTS = "exists"
    MISSING = "missing"
    UNVERIFIED = "unverified"


def _lookup_batch(names: tuple[str, ...]) -> dict[str, TwitchLookupStatus]:
    fallback = {name: TwitchLookupStatus.UNVERIFIED for name in names}
    payload = []
    for name in names:
        operation = copy.deepcopy(GQLOperations.GetIDFromLogin)
        operation["variables"]["login"] = name
        payload.append(operation)

    request = Request(
        GQLOperations.url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Client-Id": CLIENT_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TWITCH_LOOKUP_TIMEOUT_SECONDS) as response:
            if response.getcode() != 200:
                return fallback
            decoded = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError):
        return fallback

    if not isinstance(decoded, list) or len(decoded) != len(names):
        return fallback

    statuses: dict[str, TwitchLookupStatus] = {}
    for name, item in zip(names, decoded, strict=True):
        if not isinstance(item, dict):
            statuses[name] = TwitchLookupStatus.UNVERIFIED
            continue
        if item.get("errors"):
            statuses[name] = TwitchLookupStatus.UNVERIFIED
            continue
        data = item.get("data")
        if not isinstance(data, dict) or "user" not in data:
            statuses[name] = TwitchLookupStatus.UNVERIFIED
            continue
        user = data["user"]
        if user is None:
            statuses[name] = TwitchLookupStatus.MISSING
        elif isinstance(user, dict) and user.get("id"):
            statuses[name] = TwitchLookupStatus.EXISTS
        else:
            statuses[name] = TwitchLookupStatus.UNVERIFIED
    return statuses


def lookup_twitch_names(names: Iterable[str]) -> dict[str, TwitchLookupStatus]:
    """Return one tri-state result per unique name while preserving input order."""

    unique_names: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = raw_name.strip()
        folded = name.casefold()
        if name and folded not in seen:
            unique_names.append(name)
            seen.add(folded)

    results: dict[str, TwitchLookupStatus] = {}
    for offset in range(0, len(unique_names), TWITCH_LOOKUP_BATCH_SIZE):
        batch = tuple(unique_names[offset : offset + TWITCH_LOOKUP_BATCH_SIZE])
        results.update(_lookup_batch(batch))
    return results
