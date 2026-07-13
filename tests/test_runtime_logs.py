from __future__ import annotations

import io
import logging

from django.test import override_settings

from controller.miner_supervisor import _drain_miner_output
from controller.runtime_logs import MAX_LOG_BYTES, MAX_LOG_LINES, read_runtime_log_tail


def test_miner_output_is_prefixed_redacted_and_emitted_once(caplog):
    caplog.set_level(logging.INFO, logger="twitch_farm.miner_output")

    _drain_miner_output(
        io.StringIO(
            "\x1b[32mconnected\x1b[0m\n"
            "\x1b[31mpassword\x1b[0m=hunter2\n"
            "Authorization: Bearer bearer-secret\n"
            'Proxy-Authorization: Digest username="demo", response="digest-secret"\n'
            '{"password": "quoted-secret"}\n'
            "access_token='token with spaces'\n"
        ),
        "primary",
    )

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "miner[primary] connected",
        "miner[primary] password=[redacted]",
        "miner[primary] Authorization: [redacted]",
        "miner[primary] Proxy-Authorization: [redacted]",
        'miner[primary] {"password": [redacted]}',
        "miner[primary] access_token=[redacted]",
    ]


def test_runtime_log_tail_reads_rotation_in_order_and_bounds_output(tmp_path):
    path = tmp_path / "twitch-farm.log"
    path.with_suffix(".log.1").write_text("older-one\nolder-two\n", encoding="utf-8")
    path.write_text(
        "\n".join(f"current-{index}" for index in range(MAX_LOG_LINES + 25)) + "\n",
        encoding="utf-8",
    )

    with override_settings(TWITCH_FARM_LOG_FILE=path):
        lines = read_runtime_log_tail()

    assert len(lines) == MAX_LOG_LINES
    assert lines[-1] == f"current-{MAX_LOG_LINES + 24}"
    assert len("\n".join(lines).encode("utf-8")) <= MAX_LOG_BYTES


def test_runtime_log_tail_returns_empty_when_log_is_missing(tmp_path):
    with override_settings(TWITCH_FARM_LOG_FILE=tmp_path / "missing.log"):
        assert read_runtime_log_tail() == []
