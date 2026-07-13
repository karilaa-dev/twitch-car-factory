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
    first_batch_names = ["twitch", *(f"channel_{index}" for index in range(49))]
    responses = [
        FakeResponse(
            [{"data": {"user": {"id": str(index)}}} for index in range(50)]
        ),
        FakeResponse([{"data": {"user": None}}]),
    ]
    with patch("controller.twitch_lookup.urlopen", side_effect=responses) as urlopen:
        result = lookup_twitch_names(
            [*first_batch_names, "missing_channel", "TWITCH"]
        )

    expected = {
        name: TwitchLookupStatus.EXISTS for name in first_batch_names
    }
    expected["missing_channel"] = TwitchLookupStatus.MISSING
    assert result == expected
    assert urlopen.call_count == 2

    payloads = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
    assert [len(payload) for payload in payloads] == [50, 1]
    assert [item["variables"]["login"] for item in payloads[0]] == first_batch_names
    assert [item["variables"]["login"] for item in payloads[1]] == [
        "missing_channel"
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
