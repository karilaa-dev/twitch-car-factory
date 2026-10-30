from __future__ import annotations

import io
import json
from pathlib import Path
import pickle
import stat
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest

from controller.twitch_miner import auth


class FakeStop:
    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.cancelled = False

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.cancelled


def cookies(path, token="stored-secret", login="alice"):
    path.write_bytes(pickle.dumps([
        {"name": "auth-token", "value": token},
        {"name": "login", "value": login},
    ]))


def valid(login="alice", client_id="original-client"):
    return 200, {"login": login, "user_id": "42", "client_id": client_id}


def device(expires_in=30):
    return 200, {
        "device_code": "private-device-secret", "user_code": "ABCD1234",
        "verification_uri": "https://www.twitch.tv/activate", "expires_in": expires_in,
        "interval": 5,
    }


def connect(tmp_path, responses, *, method="twitch_tv", stopped=None):
    stopped = stopped or FakeStop()
    request = Mock(side_effect=responses)
    emit = Mock()
    session = auth.authenticate(
        "Alice", method, tmp_path / "Alice.pkl", emit, stopped,
        request=request, clock=lambda: stopped.now,
    )
    return session, request, emit, stopped


def test_saved_legacy_session_uses_validated_identity_and_client_id(tmp_path):
    cookies(tmp_path / "Alice.pkl")
    session, request, emit, _ = connect(tmp_path, [valid()], method="legacy_password")
    assert session.user_id == "42"
    assert session.client_id == "original-client"
    assert session.token == "stored-secret"
    assert "stored-secret" not in repr(session)
    assert request.call_count == 1
    emit.assert_not_called()


def test_device_flow_pending_and_slow_down_saves_validated_session(tmp_path):
    session, request, emit, stopped = connect(tmp_path, [
        device(60), (400, {"message": "authorization_pending"}),
        (400, {"error": "slow_down"}), (200, {"access_token": "new-secret"}), valid(),
    ])
    assert session.token == "new-secret"
    assert stopped.waits == [5, 5, 10]
    assert request.call_args_list[0].kwargs["form"]["client_id"] == auth.TV_CLIENT_ID
    assert request.call_args_list[1].kwargs["form"]["device_code"] == "private-device-secret"
    emit.assert_called_once_with(
        "device_code", user_code="ABCD1234", verification_uri="https://www.twitch.tv/activate", expires_in=60
    )
    assert "private-device-secret" not in repr(emit.call_args_list)
    path = tmp_path / "Alice.pkl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth._read_token(path, "Alice") == "new-secret"
    assert list(tmp_path.glob("*.tmp")) == []


def test_expired_saved_token_gets_one_device_code(tmp_path):
    cookies(tmp_path / "Alice.pkl")
    session, request, _, _ = connect(tmp_path, [
        (401, {}), device(), (200, {"access_token": "renewed"}), valid(),
    ])
    assert session.token == "renewed"
    assert sum(call.args[1].endswith("/device") for call in request.call_args_list) == 1


def test_fresh_device_login_uses_matching_tv_profile_for_issue_and_poll(tmp_path):
    session, request, _, _ = connect(tmp_path, [
        device(), (200, {"access_token": "tv-session"}), valid(client_id=auth.TV_CLIENT_ID),
    ])
    assert session.client_id == auth.TV_CLIENT_ID
    for call in request.call_args_list[:2]:
        assert call.kwargs["form"]["client_id"] == auth.TV_CLIENT_ID
        headers = call.kwargs["headers"]
        assert headers["Client-Id"] == auth.TV_CLIENT_ID
        assert headers["User-Agent"] == auth.TV_USER_AGENT
        assert headers["Origin"] == "https://android.tv.twitch.tv"
        assert headers["Referer"] == "https://android.tv.twitch.tv/"


def test_saved_android_token_is_not_replaced_by_fresh_tv_login(tmp_path):
    from controller.twitch_miner.constants import ANDROID_CLIENT_ID

    path = tmp_path / "Alice.pkl"
    cookies(path)
    original = path.read_bytes()
    session, request, emit, _ = connect(tmp_path, [valid(client_id=ANDROID_CLIENT_ID)])
    assert session.client_id == ANDROID_CLIENT_ID
    request.assert_called_once()
    emit.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize("have_cookie", [True, False])
def test_legacy_without_valid_session_requires_explicit_tv_switch(tmp_path, have_cookie):
    if have_cookie:
        cookies(tmp_path / "Alice.pkl")
    request = Mock(return_value=(401, {}))
    with pytest.raises(auth.AuthenticationError, match="Switch this account to Twitch TV"):
        auth.authenticate("Alice", "legacy_password", tmp_path / "Alice.pkl", Mock(), FakeStop(), request=request)
    assert request.call_count == int(have_cookie)


@pytest.mark.parametrize("stored", [True, False])
def test_wrong_account_never_succeeds_or_overwrites_session(tmp_path, stored):
    path = tmp_path / "Alice.pkl"
    responses = [valid(login="mallory")]
    if stored:
        cookies(path)
        before = path.read_bytes()
    else:
        responses = [device(), (200, {"access_token": "wrong-secret"}), *responses]
    with pytest.raises(auth.AuthenticationError, match="does not match"):
        connect(tmp_path, responses)
    if stored:
        assert path.read_bytes() == before
    else:
        assert not path.exists()


def test_code_expiry_is_exact_and_never_requests_replacement(tmp_path):
    stopped = FakeStop()
    request = Mock(side_effect=[device(7), (400, {"message": "authorization_pending"})])
    with pytest.raises(auth.AuthenticationError, match="expired"):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", Mock(), stopped, request=request, clock=lambda: stopped.now)
    assert stopped.waits == [5, 2]
    assert request.call_count == 2
    assert request.call_args_list[1].kwargs["timeout"] == 2


@pytest.mark.parametrize("error", ["access_denied", "expired_token", "invalid_device_code", "unexpected-private-secret"])
def test_rejection_does_not_expose_response_or_loop_codes(tmp_path, error):
    request = Mock(side_effect=[device(), (400, {"error": error})])
    stopped = FakeStop()
    with pytest.raises(auth.AuthenticationError) as failure:
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", Mock(), stopped, request=request, clock=lambda: stopped.now)
    assert error not in str(failure.value)
    assert request.call_count == 2
    assert not (tmp_path / "Alice.pkl").exists()


def test_cancelled_flow_never_saves_token(tmp_path):
    stopped = FakeStop()
    def emit(*args, **kwargs):
        stopped.cancelled = True
    request = Mock(return_value=device())
    with pytest.raises(auth.AuthenticationError, match="cancelled"):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", emit, stopped, request=request, clock=lambda: stopped.now)
    assert request.call_count == 1


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_cookie_symlinks_are_refused(tmp_path, kind):
    target = tmp_path / "target"
    target.mkdir()
    cookies(target / "Alice.pkl")
    path = tmp_path / "Alice.pkl"
    if kind == "file":
        path.symlink_to(target / "Alice.pkl")
    else:
        link = tmp_path / "linked"
        link.symlink_to(target, target_is_directory=True)
        path = link / "Alice.pkl"
    with pytest.raises(auth.AuthenticationError, match="symbolic"):
        auth.authenticate("Alice", "twitch_tv", path, Mock(), FakeStop(), request=Mock())


def test_executable_pickle_is_rejected_without_execution(tmp_path):
    class Unsafe:
        def __reduce__(self):
            return (Path.write_text, (tmp_path / "executed", "bad"))
    path = tmp_path / "Alice.pkl"
    path.write_bytes(pickle.dumps(Unsafe()))
    with pytest.raises(auth.AuthenticationError, match="invalid"):
        auth._read_token(path, "Alice")
    assert not (tmp_path / "executed").exists()


def test_validation_outage_does_not_start_device_flow(tmp_path):
    cookies(tmp_path / "Alice.pkl")
    request = Mock(return_value=(503, {"message": "private-secret"}))
    with pytest.raises(auth.AuthenticationError, match="try again later"):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", Mock(), FakeStop(), request=request)
    assert request.call_count == 1


def test_transport_preserves_oauth_error_status_without_body_in_exception(monkeypatch):
    error = HTTPError("https://id.twitch.tv/oauth2/token", 400, "bad", {}, io.BytesIO(json.dumps({"error": "authorization_pending"}).encode()))
    opener = Mock(side_effect=error)
    monkeypatch.setattr(auth, "urlopen", opener)
    assert auth._request_json("POST", "https://id.twitch.tv/oauth2/token", form={"device_code": "secret"}) == (400, {"error": "authorization_pending"})
    assert opener.call_args.kwargs["timeout"] == auth.HTTP_TIMEOUT
    monkeypatch.setattr(auth, "urlopen", Mock(side_effect=URLError("private-secret")))
    with pytest.raises(auth.AuthenticationError) as failure:
        auth._request_json("GET", "https://id.twitch.tv/oauth2/validate")
    assert "private-secret" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_untrusted_activation_url_is_rejected(tmp_path):
    response = device()[1]
    response["verification_uri"] = "https://evil.test/activate"
    with pytest.raises(auth.AuthenticationError, match="activation address"):
        connect(tmp_path, [(200, response)])


@pytest.mark.parametrize("field", ["expires_in", "interval"])
@pytest.mark.parametrize("value", [None, True, False, 0, -1, 3601, 86400, "30", 0.5, 30.0, float("nan"), float("inf"), float("-inf"), [], {}])
def test_malformed_device_timing_never_emits_an_activation_event(tmp_path, field, value):
    response = device()[1]
    response[field] = value
    emit = Mock()
    request = Mock(return_value=(200, response))
    with pytest.raises(auth.AuthenticationError, match="invalid device authorization"):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", emit, FakeStop(), request=request)
    emit.assert_not_called()
    assert request.call_count == 1


@pytest.mark.parametrize("code", [None, [], {}, True, 1234, "", "ABC", "A" * 33, "abcd1234", "ABCD 1234", "ABCD\n", "ABCD_1234", "АBCD1234"])
def test_malformed_user_code_never_reaches_supervisor(tmp_path, code):
    response = device()[1]
    response["user_code"] = code
    emit = Mock()
    with pytest.raises(auth.AuthenticationError, match="invalid device authorization"):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", emit, FakeStop(), request=Mock(return_value=(200, response)))
    emit.assert_not_called()


@pytest.mark.parametrize("uri", [
    "https://www.twitch.tv/activate/", "https://www.twitch.tv/activate-bad",
    "https://user@www.twitch.tv/activate", "https://www.twitch.tv:443/activate",
    "https://[invalid/activate", "https://www.twitch.tv/\nactivate",
    "https://www.twitch.tv/activate?" + "a" * 1024,
])
def test_malformed_verification_address_never_reaches_supervisor(tmp_path, uri):
    response = device()[1]
    response["verification_uri"] = uri
    emit = Mock()
    with pytest.raises(auth.AuthenticationError, match="invalid activation address"):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", emit, FakeStop(), request=Mock(return_value=(200, response)))
    emit.assert_not_called()


@pytest.mark.parametrize("response", [None, [], "private-secret", 42])
def test_malformed_device_response_shape_is_sanitized(tmp_path, response):
    with pytest.raises(auth.AuthenticationError, match="invalid device authorization"):
        connect(tmp_path, [(200, response)])


@pytest.mark.parametrize("expires_in", [1, 3600])
def test_device_event_obeys_supervisor_schema_at_expiry_boundaries(tmp_path, expires_in):
    from controller.miner_runner import CONTROL_EVENT_PREFIX
    from controller.miner_supervisor import _parse_control_event

    emit = Mock()
    stopped = FakeStop()
    request = Mock(side_effect=[device(expires_in), (400, {"error": "access_denied"})])
    with pytest.raises(auth.AuthenticationError):
        auth.authenticate("Alice", "twitch_tv", tmp_path / "Alice.pkl", emit, stopped, request=request, clock=lambda: stopped.now)
    args, kwargs = emit.call_args
    event = {"event": args[0], **kwargs}
    assert _parse_control_event(CONTROL_EVENT_PREFIX + json.dumps(event)) == event
