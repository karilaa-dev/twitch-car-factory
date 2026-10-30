"""Bounded, synchronous Twitch transport for the dedicated miner child.

Only GraphQL receives the account authorization header. CDN playlist and chunk
requests carry playback URLs, never the account OAuth token. No network payload,
request URL, or upstream error text is included in exceptions or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from base64 import b64encode
import json
import re
import secrets
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from .constants import (
    ANDROID_CLIENT_ID, ANDROID_USER_AGENT, GQL_URL, TV_CLIENT_ID, TV_USER_AGENT,
    USER_AGENT, WEB_CLIENT_ID, operation_payload,
)


REQUEST_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
WATCH_BUDGET_SECONDS = 30
MAX_PLAYLIST_SEGMENTS = 30


class TwitchError(RuntimeError):
    """A sanitized protocol or transport failure suitable for supervisor logs."""


class AuthenticationError(TwitchError):
    pass


class HTTPStatusError(TwitchError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Twitch request failed with HTTP status {status}.")


@dataclass(frozen=True, repr=False)
class HTTPResponse:
    status: int
    body: bytes

    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeError:
            raise TwitchError("Twitch returned an invalid text response.") from None

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError):
            raise TwitchError("Twitch returned an invalid JSON response.") from None


class TwitchClient:
    def __init__(
        self,
        token: str = "",
        client_id: str = WEB_CLIENT_ID,
        user_id: str = "",
        *,
        transport: Callable | None = None,
        stopped: threading.Event | None = None,
    ):
        self.token = token
        self.client_id = client_id
        self.user_id = user_id
        self.device_id = secrets.token_hex(16)
        self.session_id = secrets.token_hex(16)
        self._transport = transport or urlopen
        self._playlists: dict[str, str] = {}
        self._spade_urls: dict[str, str] = {}
        self.stopped = stopped or threading.Event()

    @property
    def user_agent(self) -> str:
        # Saved sessions may belong to the previous TV client or a web login.
        # Keep the protocol profile consistent with the validated token owner.
        if self.client_id == ANDROID_CLIENT_ID:
            return ANDROID_USER_AGENT
        if self.client_id == TV_CLIENT_ID:
            return TV_USER_AGENT
        return USER_AGENT

    @property
    def client_url(self) -> str:
        return "https://android.tv.twitch.tv" if self.client_id == TV_CLIENT_ID else "https://www.twitch.tv"

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body=None,
        data: bytes | None = None,
    ) -> HTTPResponse:
        """Return HTTP statuses as data, including OAuth polling's HTTP 400."""
        if self.stopped.is_set():
            raise TwitchError("Twitch request cancelled.")
        request_headers = {"User-Agent": self.user_agent, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        try:
            request = Request(url, data=data, headers=request_headers, method=method)
            try:
                response = self._transport(request, timeout=REQUEST_TIMEOUT_SECONDS)
            except HTTPError as error:
                response = error
            with response:
                body = b"" if method == "HEAD" else response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise TwitchError("Twitch response exceeded the size limit.")
                return HTTPResponse(response.getcode(), body)
        except (URLError, OSError, ValueError):
            raise TwitchError("Twitch request failed; retry is required.") from None

    def gql(self, operation: str, variables: dict | None = None) -> dict:
        headers = {
            "Client-Id": self.client_id,
            "Client-Session-Id": self.session_id,
            "X-Device-Id": self.device_id,
            "Origin": self.client_url,
            "Referer": self.client_url + "/",
        }
        if self.token:
            headers["Authorization"] = f"OAuth {self.token}"
        response = self.request(
            "POST", GQL_URL, headers=headers,
            json_body=operation_payload(operation, variables),
        )
        if response.status == 401:
            raise AuthenticationError("Twitch authentication was rejected.")
        if response.status != 200:
            raise HTTPStatusError(response.status)
        result = response.json()
        if not isinstance(result, dict) or result.get("errors"):
            raise TwitchError("Twitch GraphQL operation failed.")
        data = result.get("data")
        if not isinstance(data, dict):
            raise TwitchError("Twitch GraphQL returned no data.")
        return data

    def stream(self, login: str) -> dict | None:
        """Return the raw user object; its stream field is null while offline."""
        data = self.gql("GetStreamInfo", {"channel": login})
        user = data.get("user")
        if user is not None and not isinstance(user, dict):
            raise TwitchError("Twitch returned invalid channel information.")
        if not user or not user.get("stream"):
            self._playlists.pop(login, None)
        return user

    def available_drop_campaigns(self, channel_id: str) -> list[dict]:
        """Return channel campaign metadata, including TV-visible timed drops."""
        data = self.gql("AvailableDrops", {"channelID": str(channel_id)})
        channel = data.get("channel") or {}
        if not isinstance(channel, dict):
            raise TwitchError("Twitch returned invalid channel campaign information.")
        campaigns = channel.get("viewerDropCampaigns") or []
        if not isinstance(campaigns, list):
            raise TwitchError("Twitch returned invalid channel campaign information.")
        return [item for item in campaigns if isinstance(item, dict) and item.get("id")]

    def available_campaigns(self, channel_id: str) -> set[str]:
        return {str(item["id"]) for item in self.available_drop_campaigns(channel_id)}

    def claim_points(self, login: str) -> bool:
        data = self.gql("ChannelPointsContext", {"channelLogin": login})
        channel = (data.get("community") or {}).get("channel") or {}
        points = (channel.get("self") or {}).get("communityPoints") or {}
        claim = points.get("availableClaim") or {}
        if not claim.get("id") or not channel.get("id"):
            return False
        result = self.gql("ClaimCommunityPoints", {
            "input": {"claimID": claim["id"], "channelID": str(channel["id"])},
        })
        claimed = result.get("claimCommunityPoints")
        return isinstance(claimed, dict) and not claimed.get("error")

    @staticmethod
    def _twitch_owned_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
            return (
                parsed.scheme == "https"
                and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and any(hostname == root or hostname.endswith("." + root)
                        for root in ("twitch.tv", "twitchcdn.net"))
            )
        except ValueError:
            return False

    def _spade_url(self, login: str) -> str | None:
        """Find Twitch's telemetry endpoint in the page or its settings script."""
        if login in self._spade_urls:
            return self._spade_urls[login]
        page = self.request("GET", "https://www.twitch.tv/" + quote(login, safe=""))
        if page.status != 200:
            return None
        content = page.text()
        spade_pattern = r'"spade_?url"\s*:\s*"(https://[.\w\-/]+)"'
        match = re.search(spade_pattern, content, re.IGNORECASE)
        if match is None:
            script = re.search(
                r'src="(https://[\w.]+/config/settings\.[0-9a-f]{32}\.js)"',
                content, re.IGNORECASE,
            )
            if script is None or not self._twitch_owned_url(script.group(1)):
                return None
            response = self.request("GET", script.group(1))
            if response.status != 200:
                return None
            match = re.search(spade_pattern, response.text(), re.IGNORECASE)
        if match is None or not self._twitch_owned_url(match.group(1)):
            return None
        self._spade_urls[login] = match.group(1)
        return match.group(1)

    def send_points_watch(
        self, login: str, channel_id: str, broadcast_id: str,
        game_id: str, game_name: str,
    ) -> bool:
        """Send one minute of watch telemetry for channel points.

        Preserve the previous points miner's event schema and twenty-second
        playback cadence. Twitch determines credited watch time; the event
        name does not mean that the client can award a minute of points.
        """
        success = False
        try:
            if not self.user_id or not channel_id or not broadcast_id:
                return False
            endpoint = self._spade_url(login)
            if endpoint is None:
                return False
            payload = [{
                "event": "minute-watched",
                "properties": {
                    "broadcast_id": str(broadcast_id),
                    "channel_id": str(channel_id),
                    "channel": login,
                    "player": "site",
                    "game": game_name,
                    "game_id": str(game_id),
                    "live": True,
                    "user_id": int(self.user_id) if self.user_id.isdecimal() else self.user_id,
                },
            }]
            encoded = b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
            response = self.request(
                "POST", endpoint,
                data=urlencode({"data": encoded}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            success = response.status == 204
            return success
        except TwitchError:
            return False
        finally:
            if not success:
                self._spade_urls.pop(login, None)

    @staticmethod
    def _playlist_urls(body: str, base: str) -> list[str]:
        if not body.lstrip().startswith("#EXTM3U"):
            return []
        urls = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = urljoin(base, line)
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username:
                urls.append(url)
        return urls

    def _stream_playlist(self, login: str) -> str | None:
        if login in self._playlists:
            return self._playlists[login]
        data = self.gql("PlaybackAccessToken", {"login": login})
        token = data.get("streamPlaybackAccessToken") or {}
        if not token.get("value") or not token.get("signature"):
            return None
        url = (
            "https://usher.ttvnw.net/api/channel/hls/"
            + quote(login, safe="") + ".m3u8?"
            + urlencode({"sig": token["signature"], "token": token["value"]})
        )
        response = self.request("GET", url)
        if response.status != 200:
            return None
        qualities = self._playlist_urls(response.text(), url)
        if not qualities:
            return None
        # Upstream uses the final quality, normally audio-only or the lowest rate.
        self._playlists[login] = qualities[-1]
        return qualities[-1]

    def reset_playback(self, login: str) -> None:
        """Refresh playback authorization after confirmed progress stalls."""
        self._playlists.pop(login, None)

    def watch(self, login: str) -> bool:
        """Fetch HLS metadata and HEAD each segment without downloading video.

        Call around every twenty seconds, matching TwitchDropsMiner's current
        method. Failure invalidates the cached playlist for the next attempt.
        """
        success = False
        deadline = time.monotonic() + WATCH_BUDGET_SECONDS
        try:
            playlist = self._stream_playlist(login)
            if playlist is None or time.monotonic() >= deadline:
                return False
            response = self.request("GET", playlist, headers={"Connection": "close"})
            if response.status != 200:
                return False
            chunks = self._playlist_urls(response.text(), playlist)
            if not chunks or len(chunks) > MAX_PLAYLIST_SEGMENTS:
                return False
            for chunk in chunks:
                if time.monotonic() >= deadline:
                    return False
                if self.request("HEAD", chunk).status != 200:
                    return False
            success = True
            return True
        except AuthenticationError:
            raise
        except TwitchError:
            return False
        finally:
            if not success:
                self._playlists.pop(login, None)
