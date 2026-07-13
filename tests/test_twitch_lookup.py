from __future__ import annotations

import json
from urllib.error import HTTPError
from unittest.mock import patch

from controller.twitch_lookup import TwitchLookupStatus, lookup_twitch_names


class FakeResponse:
    def __init__(self, payload, *, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def test_lookup_batches_names_and_distinguishes_existing_from_missing():
    response = FakeResponse(
        [
            {"data": {"user": {"id": "12826"}}},
            {"data": {"user": None}},
        ]
    )
    with patch("controller.twitch_lookup.urlopen", return_value=response) as urlopen:
        result = lookup_twitch_names(["twitch", "missing_channel", "TWITCH"])

    assert result == {
        "twitch": TwitchLookupStatus.EXISTS,
        "missing_channel": TwitchLookupStatus.MISSING,
    }
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert [item["variables"]["login"] for item in payload] == [
        "twitch",
        "missing_channel",
    ]


def test_lookup_marks_timeout_as_unverified():
    with patch("controller.twitch_lookup.urlopen", side_effect=TimeoutError):
        result = lookup_twitch_names(["temporarily_unknown"])

    assert result == {"temporarily_unknown": TwitchLookupStatus.UNVERIFIED}


def test_lookup_marks_http_failure_as_unverified():
    error = HTTPError("https://gql.twitch.tv/gql", 503, "unavailable", {}, None)
    with patch("controller.twitch_lookup.urlopen", side_effect=error):
        result = lookup_twitch_names(["temporarily_unknown"])

    assert result == {"temporarily_unknown": TwitchLookupStatus.UNVERIFIED}


def test_lookup_marks_malformed_response_as_unverified():
    with patch(
        "controller.twitch_lookup.urlopen",
        return_value=FakeResponse(b"not-json"),
    ):
        result = lookup_twitch_names(["temporarily_unknown"])

    assert result == {"temporarily_unknown": TwitchLookupStatus.UNVERIFIED}


def test_lookup_marks_only_malformed_batch_item_as_unverified():
    response = FakeResponse(
        [
            {"unexpected": True},
            {"data": {"user": {"id": "12826"}}},
        ]
    )
    with patch("controller.twitch_lookup.urlopen", return_value=response):
        result = lookup_twitch_names(["unknown_shape", "twitch"])

    assert result == {
        "unknown_shape": TwitchLookupStatus.UNVERIFIED,
        "twitch": TwitchLookupStatus.EXISTS,
    }
