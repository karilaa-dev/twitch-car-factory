"""Worker-only Twitch session validation and bounded device authorization.

Fresh device authorization uses Twitch's TV client. Existing sessions retain
the client that issued their token, as reported by validation. No password
or authorization token crosses the supervisor event boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import pickle
import re
import secrets
import stat
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


from .constants import TV_CLIENT_ID, TV_USER_AGENT


OAUTH_BASE = "https://id.twitch.tv/oauth2"
HTTP_TIMEOUT = 10
MAX_COOKIE_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


class AuthenticationError(RuntimeError):
    """An authentication failure whose message contains no upstream payload."""


@dataclass(frozen=True, slots=True)
class Session:
    token: str = field(repr=False)
    user_id: str
    client_id: str


def _request_json(method, url, *, headers=None, form=None, timeout=HTTP_TIMEOUT):
    request_headers = {"Accept": "application/json", **(headers or {})}
    body = None
    if form is not None:
        body = urlencode(form).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.getcode()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return status, value
    except (URLError, OSError, ValueError, UnicodeError):
        raise AuthenticationError("Twitch authentication service could not be reached safely.") from None


def _safe_cookie_path(path: Path) -> None:
    # Runtime directories are worker-owned; reject links before reads or writes.
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise AuthenticationError("Runtime cookie storage cannot use symbolic links.")


def _read_token(path: Path, username: str) -> str | None:
    _safe_cookie_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise AuthenticationError("The saved Twitch session could not be read.") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise AuthenticationError("The saved Twitch session is not a regular file.")
            raw = stream.read(MAX_COOKIE_BYTES + 1)
        if len(raw) > MAX_COOKIE_BYTES:
            raise ValueError
        # The legacy importer rejects executable pickle opcodes, object globals,
        # recursive structures, and unbounded cookie values.
        from controller.legacy_import import _normalize_cookie_pickle

        cookie = _normalize_cookie_pickle(raw, username)
        values = {name.casefold(): value for name, value in cookie.values}
        return values["auth-token"]
    except AuthenticationError:
        raise
    except Exception:
        raise AuthenticationError("The saved Twitch session is invalid; reconnect Twitch.") from None


def _save_token(path: Path, username: str, token: str) -> None:
    _safe_cookie_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            pickle.dump(
                [{"name": "auth-token", "value": token}, {"name": "login", "value": username}],
                stream,
                protocol=4,
            )
            stream.flush()
            os.fsync(stream.fileno())
        _safe_cookie_path(path)
        os.replace(temporary, path)
    except OSError:
        raise AuthenticationError("The Twitch session could not be saved.") from None
    finally:
        temporary.unlink(missing_ok=True)


def _validate(token: str, username: str, request: Callable) -> Session | None:
    status, value = request(
        "GET", f"{OAUTH_BASE}/validate", headers={"Authorization": f"OAuth {token}"}
    )
    if status == 401:
        return None
    if status != 200:
        raise AuthenticationError("Twitch session validation failed; try again later.")
    if not isinstance(value, dict):
        raise AuthenticationError("Twitch returned an invalid session validation response.")
    login, user_id, client_id = (value.get(key) for key in ("login", "user_id", "client_id"))
    if not all(isinstance(item, str) and item for item in (login, user_id, client_id)):
        raise AuthenticationError("Twitch returned an invalid session validation response.")
    if login.casefold() != username.casefold():
        raise AuthenticationError("The connected Twitch account does not match this account's username.")
    return Session(token=token, user_id=user_id, client_id=client_id)


def _positive_integer(value, name: str) -> int:
    # Both the OAuth device response and supervisor event schema use integer
    # seconds. Reject fractional values, NaN, infinity, booleans, and strings.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise AuthenticationError(f"Twitch returned an invalid device authorization {name}.")
    return value


def authenticate(
    username: str,
    auth_method: str,
    cookie_path: Path,
    emit: Callable,
    stopped: threading.Event,
    *,
    request: Callable | None = None,
    clock: Callable = time.monotonic,
) -> Session:
    """Reuse a valid session, otherwise request exactly one TV activation code.

    The caller emits ``authenticated`` only after this function returns. A
    rejected, expired, or cancelled flow never requests another code in this run.
    The injected request returns ``(HTTP status, decoded JSON object)``.
    """
    request = request or _request_json
    cookie_path = Path(cookie_path)
    if stopped.is_set():
        raise AuthenticationError("Twitch authentication was cancelled.")
    token = _read_token(cookie_path, username)
    if token:
        session = _validate(token, username, request)
        if session is not None:
            return session
    if auth_method != "twitch_tv":
        raise AuthenticationError("The saved Twitch session has expired or is missing. Switch this account to Twitch TV login.")

    headers = {
        "Client-Id": TV_CLIENT_ID,
        "User-Agent": TV_USER_AGENT,
        "Origin": "https://android.tv.twitch.tv",
        "Referer": "https://android.tv.twitch.tv/",
        "X-Device-Id": secrets.token_hex(16),
    }
    requested_at = clock()
    status, device = request(
        "POST", f"{OAUTH_BASE}/device", headers=headers,
        form={"client_id": TV_CLIENT_ID, "scopes": ""},
    )
    if status != 200:
        raise AuthenticationError("Twitch device authorization could not be started.")
    if not isinstance(device, dict):
        raise AuthenticationError("Twitch returned an invalid device authorization response.")
    device_code, user_code, verification_uri = (
        device.get(key) for key in ("device_code", "user_code", "verification_uri")
    )
    if not all(isinstance(item, str) and 0 < len(item) <= 2048 for item in (device_code, user_code, verification_uri)):
        raise AuthenticationError("Twitch returned an invalid device authorization response.")
    if not re.fullmatch(r"[A-Z0-9-]{4,32}", user_code):
        raise AuthenticationError("Twitch returned an invalid device authorization user code.")
    if len(verification_uri) > 1024 or any(character.isspace() for character in verification_uri):
        raise AuthenticationError("Twitch returned an invalid activation address.")
    try:
        parsed_uri = urlsplit(verification_uri)
    except ValueError:
        raise AuthenticationError("Twitch returned an invalid activation address.") from None
    if parsed_uri.scheme != "https" or parsed_uri.netloc != "www.twitch.tv" or parsed_uri.path != "/activate":
        raise AuthenticationError("Twitch returned an invalid activation address.")
    expires_in = _positive_integer(device.get("expires_in"), "expiry")
    interval = _positive_integer(device.get("interval", 5), "poll interval")
    deadline = requested_at + expires_in
    emit("device_code", user_code=user_code, verification_uri=verification_uri, expires_in=expires_in)
    form = {
        "client_id": TV_CLIENT_ID,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise AuthenticationError("Twitch activation code expired; reconnect Twitch.")
        if stopped.wait(min(interval, remaining)):
            raise AuthenticationError("Twitch authentication was cancelled.")
        remaining = deadline - clock()
        if remaining <= 0:
            raise AuthenticationError("Twitch activation code expired; reconnect Twitch.")
        status, value = request(
            "POST", f"{OAUTH_BASE}/token", headers=headers, form=form,
            timeout=min(HTTP_TIMEOUT, remaining),
        )
        if clock() >= deadline:
            raise AuthenticationError("Twitch activation code expired; reconnect Twitch.")
        if not isinstance(value, dict):
            raise AuthenticationError("Twitch returned an invalid device authorization response.")
        if status == 200:
            token = value.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 16384:
                raise AuthenticationError("Twitch returned an invalid access token.")
            session = _validate(token, username, request)
            if session is None:
                raise AuthenticationError("Twitch rejected the activated session.")
            if stopped.is_set():
                raise AuthenticationError("Twitch authentication was cancelled.")
            _save_token(cookie_path, username, token)
            return session
        code = value.get("error") or value.get("message")
        if status == 400 and code == "authorization_pending":
            continue
        if status in (400, 429) and code == "slow_down":
            interval += 5
            continue
        if code in ("expired_token", "invalid_device_code"):
            raise AuthenticationError("Twitch activation code expired; reconnect Twitch.")
        raise AuthenticationError("Twitch device authorization was rejected; reconnect Twitch.")
