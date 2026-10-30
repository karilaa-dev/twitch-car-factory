"""Upgrade contracts for TV sessions written by the previous dependency.

The fixture matches TwitchLogin.save_cookies at the formerly pinned revision:
https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/blob/8a35f87cd5345074003c7f6eedb48aedadc3b9b3/TwitchChannelPointsMiner/classes/TwitchLogin.py
TV login normally persisted auth-token and persistent, with no login cookie.
"""

import pickle
import threading
from unittest.mock import Mock

import pytest

from controller import miner_runner
from controller.crypto import encrypt_json
from controller.models import (
    AccountChannelSelection,
    AccountCredential,
    AccountSessionSeed,
    FarmConfiguration,
    MinerAccount,
)
from controller.services import create_launch_snapshot
from controller.twitch_miner import auth, engine
from controller.twitch_miner.constants import TV_CLIENT_ID, TV_USER_AGENT


OLD_TOKEN = "synthetic-previous-version-tv-token"
OLD_TV_CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
OLD_COOKIES = [
    {"name": "auth-token", "value": OLD_TOKEN},
    {"name": "persistent", "value": "42"},
]


def validate_old_tv_session(method, url, *, headers):
    # An unexpected device-code request fails this fixture immediately.
    assert method == "GET"
    assert url == "https://id.twitch.tv/oauth2/validate"
    assert headers == {"Authorization": f"OAuth {OLD_TOKEN}"}
    return 200, {"login": "alice", "user_id": "42", "client_id": OLD_TV_CLIENT_ID}


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", [4, 5])
def test_previous_tv_account_starts_after_upgrade_without_reconnecting(
    tmp_path, monkeypatch, protocol
):
    account = MinerAccount.objects.create(config_key="old-tv", display_username="Alice")
    credential = AccountCredential.objects.create(account=account, auth_method="twitch_tv")
    AccountChannelSelection.objects.create(account=account)
    configuration = FarmConfiguration.load()
    configuration.default_channels = ["configured_channel"]
    configuration.save()
    run = create_launch_snapshot(account, worker_id="new-version-worker")
    assert not run.reset_session
    payload = miner_runner.load_launch_payload(run.pk, account.pk)
    assert payload.auth_method == "twitch_tv"
    assert payload.password == ""

    runtime = tmp_path / "runtime"
    cookies = runtime / "cookies"
    cookies.mkdir(parents=True)
    destination = cookies / "Alice.pkl"
    old_bytes = pickle.dumps(OLD_COOKIES, protocol=protocol)
    destination.write_bytes(old_bytes)
    old_mtime = destination.stat().st_mtime_ns
    monkeypatch.chdir(runtime)

    prepared = miner_runner.prepare_runtime_cookie("Alice", account.pk)
    assert prepared == destination
    assert destination.read_bytes() == old_bytes
    assert destination.stat().st_mtime_ns == old_mtime
    assert destination.stat().st_mode & 0o777 == 0o600

    validation = Mock(side_effect=validate_old_tv_session)
    events = []
    miner = Mock()
    monkeypatch.setattr(auth, "_request_json", validation)
    monkeypatch.setattr(engine, "DropsMiner", miner)
    monkeypatch.setattr(miner_runner, "prepare_upstream_logging", lambda: None)
    monkeypatch.setattr(miner_runner.logging, "basicConfig", lambda **_kwargs: None)
    monkeypatch.setattr(
        miner_runner, "emit_control_event",
        lambda event, **payload: events.append((event, payload)),
    )

    miner_runner.run_miner(payload)

    validation.assert_called_once()
    miner.return_value.run.assert_called_once()
    protocol_client = miner.call_args.args[0]
    assert protocol_client.token == OLD_TOKEN
    assert protocol_client.user_id == "42"
    assert protocol_client.client_id == TV_CLIENT_ID == OLD_TV_CLIENT_ID
    assert protocol_client.user_agent == TV_USER_AGENT
    assert protocol_client.client_url == "https://android.tv.twitch.tv"
    assert miner.call_args.args[1:3] == ("Alice", ("configured_channel",))
    assert events[0] == ("authenticated", {})
    assert all(event != "device_code" for event, _ in events)
    assert OLD_TOKEN not in repr(events)
    assert destination.read_bytes() == old_bytes
    assert destination.stat().st_mtime_ns == old_mtime
    credential.refresh_from_db()
    assert credential.auth_method == "twitch_tv"
    assert credential.password_ciphertext == ""


@pytest.mark.django_db
def test_previous_tv_cookie_seed_is_consumed_without_requesting_device_login(
    tmp_path, monkeypatch
):
    account = MinerAccount.objects.create(config_key="imported-tv", display_username="Alice")
    AccountCredential.objects.create(account=account, auth_method="twitch_tv")
    AccountSessionSeed.objects.create(
        account=account, payload_ciphertext=encrypt_json(OLD_COOKIES),
    )
    monkeypatch.chdir(tmp_path)
    path = miner_runner.prepare_runtime_cookie("Alice", account.pk)
    assert not AccountSessionSeed.objects.filter(account=account).exists()
    original = path.read_bytes()
    emit = Mock()
    validation = Mock(side_effect=validate_old_tv_session)

    session = auth.authenticate(
        "Alice", "twitch_tv", path, emit, threading.Event(), request=validation,
    )

    assert session.token == OLD_TOKEN
    assert session.client_id == OLD_TV_CLIENT_ID
    validation.assert_called_once()
    emit.assert_not_called()
    assert path.read_bytes() == original
