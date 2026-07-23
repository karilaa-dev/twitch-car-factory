from __future__ import annotations

import gzip
import io
import logging
import os
import queue
import stat
from unittest.mock import patch

import pytest
from django.test import override_settings

from controller.miner_runner import CONTROL_EVENT_PREFIX
from controller.miner_supervisor import _drain_miner_output
from controller import runtime_logs
from controller.runtime_logs import (
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    AccountRunLogWriter,
    LogStorageError,
    iter_run_gzip,
    read_account_live,
    read_combined_live,
    read_run_log_page,
    read_runtime_log_tail,
    recover_account_log_archives,
    summarize_run_log,
)


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


def test_miner_output_drops_debug_protocol_payloads_from_all_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="twitch_farm.miner_output")
    path = tmp_path / "logs" / "farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=7, run_id=92, account_key="primary")
        _drain_miner_output(
            io.StringIO(
                "2026-07-22 23:44:00,743 DEBUG TwitchChannelPointsMiner.classes.Twitch: Data: secret payload\n"
                "22/07/26 23:44:00 - DEBUG - [send]: websocket payload\n"
                "22/07/26 23:44:01 - INFO - [run]: [primary] Loading data\n"
            ),
            "primary",
            writer,
        )
        writer.finalize("done")
        page = read_run_log_page(account_id=7, run_id=92)

    rendered = "\n".join(page["lines"])
    assert "secret payload" not in rendered
    assert "websocket payload" not in rendered
    assert "Loading data" in rendered
    assert [record.getMessage() for record in caplog.records] == [
        "miner[primary] 22/07/26 23:44:01 - INFO - [run]: [primary] Loading data"
    ]


def test_control_events_are_validated_and_never_written_as_library_output(tmp_path, caplog):
    path = tmp_path / "logs" / "farm.log"
    events = queue.SimpleQueue()
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=7, run_id=91, account_key="primary")
        _drain_miner_output(
            io.StringIO(
                CONTROL_EVENT_PREFIX
                + '{"event":"device_code","user_code":"ABCD-1234","verification_uri":"https://www.twitch.tv/activate","expires_in":1800}\n'
                + CONTROL_EVENT_PREFIX
                + '{"event":"authenticated","access_token":"must-not-pass"}\n'
                + "\u001b[32mupstream info\u001b[0m\n"
            ),
            "primary",
            writer,
            events,
        )
        writer.finalize("done")
        page = read_run_log_page(account_id=7, run_id=91)

    event = events.get_nowait()
    assert event["event"] == "device_code"
    assert events.empty()
    rendered = "\n".join(page["lines"])
    assert "upstream info" in rendered
    assert "INFO library account=primary run=91: upstream info" in rendered
    assert "must-not-pass" not in rendered
    assert "control_event_rejected" in rendered


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


def test_runtime_log_tail_enforces_byte_cap_after_utf8_replacement(tmp_path):
    path = tmp_path / "twitch-farm.log"
    path.write_bytes(b"\xff" * MAX_LOG_BYTES)

    with override_settings(TWITCH_FARM_LOG_FILE=path):
        lines = read_runtime_log_tail()

    assert lines
    assert len("\n".join(lines).encode("utf-8")) <= MAX_LOG_BYTES


def test_combined_initial_cursor_keeps_an_append_after_its_snapshot(tmp_path):
    path = tmp_path / "twitch-farm.log"
    path.write_text("initial-line\n", encoding="utf-8")
    original_snapshot = runtime_logs._read_runtime_log_snapshot

    def snapshot_then_append():
        snapshot = original_snapshot()
        with path.open("a", encoding="utf-8") as stream:
            stream.write("line-appended-after-snapshot\n")
        return snapshot

    with override_settings(TWITCH_FARM_LOG_FILE=path):
        with patch(
            "controller.runtime_logs._read_runtime_log_snapshot",
            side_effect=snapshot_then_append,
        ):
            initial = read_combined_live()
        incremental = read_combined_live(initial["cursor"])

    assert initial["lines"] == ["initial-line"]
    assert incremental["lines"] == ["line-appended-after-snapshot"]


def test_account_writer_collects_redacted_output_and_lifecycle_in_gzip(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(
        TWITCH_FARM_LOG_FILE=path,
        TWITCH_FARM_ACCOUNT_LOG_PART_BYTES=1024 * 1024,
        TWITCH_FARM_ACCOUNT_LOG_ARCHIVE_BYTES=50 * 1024 * 1024,
    ):
        writer = AccountRunLogWriter(account_id=7, run_id=11, account_key="primary")
        writer.lifecycle("launch_requested", channels=["one", "two"])
        _drain_miner_output(
            io.StringIO("connected\npassword=hunter2\nAuthorization: Bearer secret\n"),
            "primary",
            writer,
        )
        writer.finalize("run_finished", reason="admin_stop")
        summary = summarize_run_log(7, 11)
        archive = path.parent / "accounts" / "7" / "runs" / "11" / "part-000000.log.gz"

    assert summary.compressed_parts == 1
    assert not summary.compression_pending
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        contents = stream.read()
    assert "launch_requested" in contents
    assert "connected" in contents
    assert "password=[redacted]" in contents
    assert "Authorization: [redacted]" in contents
    assert "hunter2" not in contents
    assert "Bearer secret" not in contents
    assert "run_finished" in contents
    assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_account_writer_keeps_each_rendered_message_on_one_line(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=7, run_id=12, account_key="primary")
        writer.write("first\nsecond\rthird\tfourth\x7f")
        writer.finalize("run_finished")
        page = read_run_log_page(account_id=7, run_id=12)

    assert len(page["lines"]) == 2
    assert "first second third fourth " in page["lines"][0]


def test_run_summary_tolerates_archive_removed_after_discovery(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=7, run_id=13, account_key="primary")
        writer.write("retained until the summary starts")
        writer.finalize("run_finished")
        discovered = runtime_logs._run_parts(7, 13)
        assert len(discovered) == 1
        discovered[0].path.unlink()
        with patch("controller.runtime_logs._run_parts", return_value=discovered):
            summary = summarize_run_log(7, 13)

    assert summary.available
    assert summary.compressed_parts == 1
    assert summary.compressed_bytes == 0


def test_account_archives_rotate_prune_and_stay_isolated(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(
        TWITCH_FARM_LOG_FILE=path,
        TWITCH_FARM_ACCOUNT_LOG_PART_BYTES=180,
        TWITCH_FARM_ACCOUNT_LOG_ARCHIVE_BYTES=230,
    ):
        primary = AccountRunLogWriter(account_id=1, run_id=10, account_key="primary")
        secondary = AccountRunLogWriter(account_id=2, run_id=20, account_key="secondary")
        for index in range(30):
            primary.write(f"primary-{index:03d}-abcdefghijklmnopqrstuvwxyz-{index * 7919}")
            secondary.write(f"secondary-{index:03d}-zyxwvutsrqponmlkjihgfedcba-{index * 6151}")
        primary.finalize("run_finished")
        secondary.finalize("run_finished")
        primary_summary = summarize_run_log(1, 10)
        secondary_summary = summarize_run_log(2, 20)
        primary_page = read_run_log_page(account_id=1, run_id=10, limit=400)
        secondary_page = read_run_log_page(account_id=2, run_id=20, limit=400)

    assert primary_summary.compressed_bytes <= 230
    assert secondary_summary.compressed_bytes <= 230
    assert primary_summary.truncated
    assert secondary_summary.truncated
    assert all("secondary-" not in line for line in primary_page["lines"])
    assert all("primary-" not in line for line in secondary_page["lines"])


def test_compression_failure_leaves_plaintext_pending_and_recovery_retries(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=3, run_id=30, account_key="pending")
        writer.write("keep this line")
        with patch("controller.runtime_logs._compress_plain_part", side_effect=OSError("disk")):
            writer.finalize("run_finished")
        pending = summarize_run_log(3, 30)
        errors = recover_account_log_archives()
        recovered = summarize_run_log(3, 30)

    assert pending.compression_pending
    assert writer.last_error == "OSError"
    assert errors == []
    assert not recovered.compression_pending
    assert recovered.compressed_parts == 1


def test_account_live_cursor_and_run_paging_follow_rotated_parts(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(
        TWITCH_FARM_LOG_FILE=path,
        TWITCH_FARM_ACCOUNT_LOG_PART_BYTES=240,
    ):
        writer = AccountRunLogWriter(account_id=4, run_id=40, account_key="cursor")
        for index in range(18):
            writer.write(f"line-{index:02d}-abcdefghijk")
        initial = read_account_live(account_id=4, run_id=40)
        writer.write("line-after-cursor")
        incremental = read_account_live(
            account_id=4,
            run_id=40,
            cursor=initial["cursor"],
        )
        newest = read_run_log_page(account_id=4, run_id=40, limit=5)
        older = read_run_log_page(
            account_id=4,
            run_id=40,
            before=newest["before"],
            limit=5,
        )
        writer.finalize("run_finished")
        complete_page = read_run_log_page(account_id=4, run_id=40, limit=400)
        chunks, size = iter_run_gzip(4, 40)
        payload = b"".join(chunks)

    assert len(incremental["lines"]) == 1
    assert "line-after-cursor" in incremental["lines"][0]
    assert len(newest["lines"]) == 5
    assert older["lines"]
    assert complete_page["has_older"] is False
    assert complete_page["before"] is None
    assert size == len(payload)
    assert "line-after-cursor" in gzip.decompress(payload).decode("utf-8")


def test_run_download_survives_retention_after_parts_are_opened(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=4, run_id=42, account_key="download-race")
        writer.write("download remains readable")
        writer.finalize("run_finished")
        archive = runtime_logs._run_parts(4, 42)[0].path
        chunks, size = iter_run_gzip(4, 42)
        archive.unlink()
        payload = b"".join(chunks)

    assert size == len(payload)
    assert "download remains readable" in gzip.decompress(payload).decode("utf-8")


def test_run_download_reports_a_part_pruned_before_it_can_be_opened(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=4, run_id=43, account_key="download-race")
        writer.write("pruned before download")
        writer.finalize("run_finished")
        discovered = runtime_logs._run_parts(4, 43)
        discovered[0].path.unlink()
        with patch("controller.runtime_logs._run_parts", return_value=discovered):
            with pytest.raises(LogStorageError, match="no longer available"):
                iter_run_gzip(4, 43)


def test_account_initial_cursor_keeps_a_line_appended_after_page_read(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=4, run_id=41, account_key="cursor-race")
        writer.write("initial-account-line")
        original_page_reader = runtime_logs._run_page_from_position

        def page_then_append(**kwargs):
            page = original_page_reader(**kwargs)
            writer.write("account-line-appended-after-page")
            return page

        with patch(
            "controller.runtime_logs._run_page_from_position",
            side_effect=page_then_append,
        ):
            initial = read_account_live(account_id=4, run_id=41)
        incremental = read_account_live(
            account_id=4,
            run_id=41,
            cursor=initial["cursor"],
        )
        writer.finalize("run_finished")

    assert any("initial-account-line" in line for line in initial["lines"])
    assert not any("account-line-appended-after-page" in line for line in initial["lines"])
    assert len(incremental["lines"]) == 1
    assert "account-line-appended-after-page" in incremental["lines"][0]


def test_recovery_skips_active_plaintext_and_rejects_symlinked_run_roots(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(TWITCH_FARM_LOG_FILE=path):
        writer = AccountRunLogWriter(account_id=5, run_id=50, account_key="active")
        writer.write("still active")
        assert recover_account_log_archives(active_run_ids=[50]) == []
        assert summarize_run_log(5, 50).compression_pending

        writer.finalize("run_finished")
        outside = tmp_path / "outside"
        outside.mkdir()
        unsafe_parent = path.parent / "accounts" / "6" / "runs"
        unsafe_parent.mkdir(parents=True)
        (unsafe_parent / "60").symlink_to(outside, target_is_directory=True)
        with pytest.raises(LogStorageError, match="unsafe"):
            summarize_run_log(6, 60)


def test_recovery_prunes_an_older_pending_candidate_before_newer_archives(tmp_path):
    path = tmp_path / "logs" / "twitch-farm.log"
    with override_settings(
        TWITCH_FARM_LOG_FILE=path,
        TWITCH_FARM_ACCOUNT_LOG_ARCHIVE_BYTES=50 * 1024 * 1024,
    ):
        old = AccountRunLogWriter(account_id=8, run_id=80, account_key="ordered")
        old.write("same-size-retention-payload")
        with patch("controller.runtime_logs._compress_plain_part", side_effect=OSError("disk")):
            old.finalize("run_finished")
        old_plaintext = path.parent / "accounts" / "8" / "runs" / "80" / "part-000000.log"
        old_timestamp = 1_000_000_000
        os.utime(old_plaintext, ns=(old_timestamp, old_timestamp))
        candidate_buffer = io.BytesIO()
        with gzip.GzipFile(
            filename=old_plaintext.name,
            fileobj=candidate_buffer,
            mode="wb",
            mtime=0,
        ) as compressed_candidate:
            compressed_candidate.write(old_plaintext.read_bytes())
        candidate_size = len(candidate_buffer.getvalue())

        new = AccountRunLogWriter(account_id=8, run_id=81, account_key="ordered")
        new.write("same-size-retention-payload")
        new.finalize("run_finished")
        new_summary = summarize_run_log(8, 81)

        with override_settings(
            TWITCH_FARM_LOG_FILE=path,
            TWITCH_FARM_ACCOUNT_LOG_ARCHIVE_BYTES=max(
                candidate_size,
                new_summary.compressed_bytes,
            ),
        ):
            assert recover_account_log_archives() == []
            old_summary = summarize_run_log(8, 80)
            retained_new = summarize_run_log(8, 81)

    assert not old_summary.available
    assert retained_new.available
