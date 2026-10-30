from __future__ import annotations

import json
import threading
from base64 import b64decode
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from controller.twitch_miner.client import (
    AuthenticationError,
    HTTPResponse,
    HTTPStatusError,
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    TwitchClient,
    TwitchError,
)
from controller.twitch_miner.constants import (
    ANDROID_CLIENT_ID, ANDROID_USER_AGENT, TV_CLIENT_ID, TV_USER_AGENT,
    USER_AGENT, WEB_CLIENT_ID, operation_payload,
)


class Response:
    def __init__(self, body=b"", status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.read_count = 0

    def getcode(self):
        return self.status

    def read(self, size):
        self.read_count += 1
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class Transport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append(request)
        assert timeout == REQUEST_TIMEOUT_SECONDS
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def gql_response(data):
    return Response({"data": data})


def test_gql_preserves_defaults_and_attaches_current_client_credentials():
    transport = Transport(gql_response({"streamPlaybackAccessToken": None}))
    client = TwitchClient("private-token", "my-client", transport=transport)
    assert client.gql("PlaybackAccessToken", {"login": "channel"}) == {"streamPlaybackAccessToken": None}
    request = transport.calls[0]
    assert request.get_header("Authorization") == "OAuth private-token"
    assert request.get_header("Client-id") == "my-client"
    payload = json.loads(request.data)
    assert payload["variables"] == {
        "login": "channel", "isLive": True, "isVod": False,
        "platform": "web", "playerType": "site", "vodID": "",
    }
    assert len(payload["extensions"]["persistedQuery"]["sha256Hash"]) == 64
    assert "login" not in operation_payload("PlaybackAccessToken")["variables"]


@pytest.mark.parametrize("client_id,user_agent,origin", [
    (ANDROID_CLIENT_ID, ANDROID_USER_AGENT, "https://www.twitch.tv"),
    (TV_CLIENT_ID, TV_USER_AGENT, "https://android.tv.twitch.tv"),
    (WEB_CLIENT_ID, USER_AGENT, "https://www.twitch.tv"),
])
def test_gql_profile_matches_validated_session_client(client_id, user_agent, origin):
    transport = Transport(gql_response({"user": None}))
    client = TwitchClient("saved-token", transport=transport)
    client.client_id = client_id
    client.gql("GetStreamInfo", {"channel": "test"})
    request = transport.calls[0]
    assert request.get_header("Client-id") == client_id
    assert request.get_header("User-agent") == user_agent
    assert request.get_header("Origin") == origin
    assert request.get_header("Authorization") == "OAuth saved-token"


@pytest.mark.parametrize("response,error_type", [
    (Response({"error": "private-token"}, 401), AuthenticationError),
    (Response(b"private-token", 503), HTTPStatusError),
    (Response({"errors": [{"message": "private-token"}]}), TwitchError),
    (Response({"data": None}), TwitchError),
    (Response(b"private-token"), TwitchError),
    (URLError("private-token"), TwitchError),
])
def test_gql_errors_are_sanitized(response, error_type):
    client = TwitchClient(transport=Transport(response))
    with pytest.raises(error_type) as error:
        client.gql("Inventory")
    assert "private-token" not in str(error.value)


def test_request_preserves_non_success_status_for_oauth_polling():
    from io import BytesIO

    error = HTTPError(
        "https://id.twitch.tv/oauth2/token", 400, "Bad Request", {},
        BytesIO(b'{"message":"authorization_pending"}'),
    )
    client = TwitchClient(transport=Transport(error))
    response = client.request("POST", "https://id.twitch.tv/oauth2/token", data=b"a=b")
    assert response.status == 400
    assert response.json() == {"message": "authorization_pending"}


def test_response_limit_and_malformed_utf8_are_sanitized():
    client = TwitchClient(transport=Transport(Response(b"x" * (MAX_RESPONSE_BYTES + 1))))
    with pytest.raises(TwitchError, match="size limit"):
        client.request("GET", "https://www.twitch.tv")
    with pytest.raises(TwitchError, match="invalid text"):
        HTTPResponse(200, b"\xff").text()


def test_watch_fetches_playlists_and_heads_every_chunk_without_account_token():
    first_chunk, second_chunk = Response(b"must not read video"), Response(b"must not read video")
    transport = Transport(
        gql_response({"streamPlaybackAccessToken": {"signature": "sig&+", "value": '{"token":"+&"}'}}),
        Response(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nhttps://cdn.twitch.tv/high.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=100\nhttps://cdn.twitch.tv/low.m3u8\n"),
        Response(b"#EXTM3U\n#EXTINF:2,\nhttps://cdn.twitch.tv/1.ts\n#EXTINF:2,\n2.ts\n"),
        first_chunk, second_chunk,
        # A later heartbeat reuses only the media playlist, never stale chunks.
        Response(b"#EXTM3U\n#EXTINF:2,\n3.ts\n"), Response(),
    )
    client = TwitchClient("private-token", transport=transport)
    assert client.watch("channel") is True
    assert client.watch("channel") is True
    assert [request.method for request in transport.calls] == ["POST", "GET", "GET", "HEAD", "HEAD", "GET", "HEAD"]
    query = parse_qs(urlsplit(transport.calls[1].full_url).query)
    assert query == {"sig": ["sig&+"], "token": ['{"token":"+&"}']}
    assert transport.calls[2].full_url == "https://cdn.twitch.tv/low.m3u8"
    assert transport.calls[4].full_url == "https://cdn.twitch.tv/2.ts"
    assert transport.calls[-1].full_url == "https://cdn.twitch.tv/3.ts"
    assert all(request.get_header("Authorization") is None for request in transport.calls[1:])
    assert first_chunk.read_count == second_chunk.read_count == 0


@pytest.mark.parametrize("response", [
    Response(b"#EXTM3U\n# no chunks"),
    Response(b'{"error":"private token"}'),
    Response(b"#EXTM3U\n1.ts", status=404),
    URLError("offline"),
])
def test_watch_failure_discards_cached_playlist(response):
    transport = Transport(response)
    client = TwitchClient(transport=transport)
    client._playlists["channel"] = "https://cdn.twitch.tv/media.m3u8"
    assert client.watch("channel") is False
    assert "channel" not in client._playlists


def test_watch_failed_chunk_does_not_report_success():
    client = TwitchClient(transport=Transport(
        Response(b"#EXTM3U\n1.ts\n2.ts\n"), Response(status=403),
    ))
    client._playlists["channel"] = "https://cdn.twitch.tv/media.m3u8"
    assert client.watch("channel") is False
    assert client._playlists == {}


def test_watch_expired_auth_is_not_mistaken_for_offline_channel():
    client = TwitchClient(transport=Transport(Response(status=401)))
    with pytest.raises(AuthenticationError):
        client.watch("channel")


def test_watch_stops_before_next_request_on_shutdown():
    stopped = threading.Event()
    stopped.set()
    transport = Transport()
    client = TwitchClient(transport=transport, stopped=stopped)
    client._playlists["channel"] = "https://cdn.twitch.tv/media.m3u8"
    assert client.watch("channel") is False
    assert transport.calls == []


def test_watch_has_whole_heartbeat_budget(monkeypatch):
    from controller.twitch_miner import client as client_module

    clock = iter([0, 0, 31])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(clock))
    transport = Transport(Response(b"#EXTM3U\n1.ts\n2.ts\n"))
    client = TwitchClient(transport=transport)
    client._playlists["channel"] = "https://cdn.twitch.tv/media.m3u8"
    assert client.watch("channel") is False
    assert len(transport.calls) == 1


def test_offline_stream_invalidates_cached_playlist_and_returns_raw_user():
    client = TwitchClient(transport=Transport(gql_response({"user": {"id": "1", "stream": None}})))
    client._playlists["channel"] = "https://cdn.twitch.tv/media.m3u8"
    assert client.stream("channel") == {"id": "1", "stream": None}
    assert client._playlists == {}


def test_available_campaigns_only_returns_ids():
    client = TwitchClient(transport=Transport(gql_response({
        "channel": {"viewerDropCampaigns": [{"id": "campaign1"}, {"id": "campaign2"}]},
    })))
    assert client.available_campaigns("123") == {"campaign1", "campaign2"}


def test_channel_points_available_bonus_is_claimed():
    transport = Transport(
        gql_response({"community": {"channel": {
            "id": "123", "self": {"communityPoints": {"availableClaim": {"id": "claim1"}}},
        }}}),
        gql_response({"claimCommunityPoints": {"claim": {"id": "claim1"}, "error": None}}),
    )
    client = TwitchClient(transport=transport)
    assert client.claim_points("channel") is True
    payload = json.loads(transport.calls[-1].data)
    assert payload["variables"] == {"input": {"claimID": "claim1", "channelID": "123"}}


def test_channel_points_without_claim_does_not_send_mutation():
    transport = Transport(gql_response({"community": {"channel": {
        "id": "123", "self": {"communityPoints": {"availableClaim": None}},
    }}}))
    assert TwitchClient(transport=transport).claim_points("channel") is False
    assert len(transport.calls) == 1


@pytest.mark.parametrize("via_script", [False, True])
def test_points_watch_discovers_endpoint_and_posts_minute_without_account_token(via_script):
    endpoint = b'"spade_url": "https://spade.twitch.tv/track"'
    responses = [Response(endpoint)]
    if via_script:
        responses.insert(0, Response(
            b'<script src="https://static.twitchcdn.net/config/settings.0123456789abcdef0123456789abcdef.js"></script>'
        ))
    transport = Transport(*responses, Response(status=204), Response(status=204))
    client = TwitchClient("private-token", user_id="42", transport=transport)
    assert client.send_points_watch("channel", "100", "200", "300", "Game") is True
    assert client.send_points_watch("channel", "100", "201", "300", "Game") is True
    assert len(transport.calls) == (4 if via_script else 3)
    for request in transport.calls:
        assert request.get_header("Authorization") is None
        assert b"private-token" not in (request.data or b"")
    post = transport.calls[-1]
    assert post.method == "POST"
    assert post.full_url == "https://spade.twitch.tv/track"
    encoded = parse_qs(post.data.decode())["data"][0]
    event = json.loads(b64decode(encoded))[0]
    assert event["event"] == "minute-watched"
    assert event["properties"] == {
        "broadcast_id": "201", "channel_id": "100", "channel": "channel", "player": "site",
        "game": "Game", "game_id": "300", "live": True, "user_id": 42,
    }


@pytest.mark.parametrize("body", [
    b'<script src="https://attacker.test/config/settings.0123456789abcdef0123456789abcdef.js"></script>',
    b'"spade_url": "https://twitch.tv.attacker.test/track"',
    b"<html>unrecognized settings</html>",
])
def test_points_watch_rejects_unknown_endpoint_or_script_without_posting(body):
    transport = Transport(Response(body))
    client = TwitchClient("private-token", user_id="42", transport=transport)
    assert client.send_points_watch("channel", "100", "200", "300", "Game") is False
    assert len(transport.calls) == 1


def test_points_watch_failure_clears_discovery_cache_and_does_not_raise():
    client = TwitchClient(user_id="42", transport=Transport(URLError("private token")))
    client._spade_urls["channel"] = "https://spade.twitch.tv/track"
    assert client.send_points_watch("channel", "100", "200", "300", "Game") is False
    assert client._spade_urls == {}



def test_available_drop_campaigns_preserves_full_metadata_for_tv_discovery():
    campaign = {"id": "campaign", "game": {"id": "game"},
                "timeBasedDrops": [{"id": "drop", "requiredMinutesWatched": 30}]}
    client = TwitchClient(transport=Transport(gql_response({
        "channel": {"viewerDropCampaigns": [campaign]},
    })))
    assert client.available_drop_campaigns("123") == [campaign]
