"""Integration contract between the supervised child and the owned engine."""
from unittest.mock import Mock

import pytest

from controller import miner_runner
from controller.twitch_miner import auth, client, engine


def test_runner_uses_owned_engine_and_emits_authentication_only_after_validation(monkeypatch, tmp_path):
    events = []
    session = auth.Session(token="private-token", user_id="42", client_id="validated-client")
    login = Mock(return_value=session)
    protocol = Mock()
    miner = Mock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "authenticate", login)
    monkeypatch.setattr(client, "TwitchClient", protocol)
    monkeypatch.setattr(engine, "DropsMiner", miner)
    monkeypatch.setattr(miner_runner, "prepare_upstream_logging", lambda: None)
    monkeypatch.setattr(miner_runner, "emit_control_event", lambda event, **payload: events.append((event, payload)))
    payload = miner_runner.LaunchPayload("Alice", "unused-password", ("Alpha", "Beta"), "twitch_tv")

    miner_runner.run_miner(payload)

    assert login.call_args.args[:3] == ("Alice", "twitch_tv", tmp_path / "cookies" / "Alice.pkl")
    assert protocol.call_args.kwargs["token"] == "private-token"
    assert protocol.call_args.kwargs["client_id"] == "validated-client"
    assert miner.call_args.args[:3] == (protocol.return_value, "Alice", ("Alpha", "Beta"))
    miner.return_value.run.assert_called_once()
    assert events[0] == ("authenticated", {})
    assert events[-1] == ("watching_channels", {"channels": [], "online_channels": []})
    assert "private-token" not in repr(events)
    assert "unused-password" not in repr(login.call_args)


@pytest.mark.parametrize("error", [auth.AuthenticationError, client.AuthenticationError])
def test_authentication_failures_reach_supervisor(monkeypatch, error):
    events = []
    monkeypatch.setattr(auth, "authenticate", Mock(side_effect=error("Twitch authentication was rejected.")))
    monkeypatch.setattr(miner_runner, "prepare_upstream_logging", lambda: None)
    monkeypatch.setattr(miner_runner, "emit_control_event", lambda event, **payload: events.append((event, payload)))
    with pytest.raises(error):
        miner_runner.run_miner(miner_runner.LaunchPayload("Alice", "", ("Alpha",), "twitch_tv"))
    assert events[0][0] == "authentication_failed"
    assert all(event != "authenticated" for event, _ in events)
