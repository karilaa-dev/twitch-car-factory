"""Run a deterministic, harmless miner stand-in for reliability testing."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from controller.miner_runner import configure_linux_parent_death_signal, emit_control_event
from controller.models import AccountCredential, MinerRun


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise CommandError(f"{name} must be a number.") from exc


class Command(BaseCommand):
    help = "Run a fake miner from an immutable launch snapshot."

    def add_arguments(self, parser) -> None:
        parser.add_argument("run_id", type=int)
        parser.add_argument("account_id", type=int)
        parser.add_argument(
            "--mode",
            choices=("normal", "exit-immediately", "crash-after", "ignore-term"),
            default=os.environ.get("TWITCH_FARM_FAKE_MINER_MODE", "normal"),
            help="Controllable process behavior used by reliability tests.",
        )
        parser.add_argument(
            "--duration",
            type=float,
            default=_env_float("TWITCH_FARM_FAKE_MINER_DURATION", 3600.0),
        )
        parser.add_argument(
            "--crash-after",
            type=float,
            default=_env_float("TWITCH_FARM_FAKE_MINER_CRASH_AFTER", 0.25),
        )
        parser.add_argument(
            "--exit-code",
            type=int,
            default=int(os.environ.get("TWITCH_FARM_FAKE_MINER_EXIT_CODE", "17")),
        )
        parser.add_argument(
            "--record-file",
            default=os.environ.get("TWITCH_FARM_FAKE_MINER_RECORD_FILE", ""),
            help="Append one JSON line describing the exact launch snapshot.",
        )

    def handle(self, *args, **options) -> None:
        configure_linux_parent_death_signal()
        run = (
            MinerRun.objects.select_related("account")
            .filter(
                pk=options["run_id"],
                account_id=options["account_id"],
                ended_at__isnull=True,
            )
            .first()
        )
        if run is None:
            raise CommandError(
                "Launch snapshot is missing, closed, or belongs to another account."
            )

        channels = list(run.channels or ())
        if not channels:
            raise CommandError("Launch snapshot has no channels.")

        mode = options["mode"]
        record = {
            "run_id": run.pk,
            "account_key": run.account.config_key,
            "channels": channels,
            "configuration_fingerprint": run.configuration_fingerprint,
            "channel_revision": run.channel_revision,
            "mode": mode,
            "pid": os.getpid(),
            "started_at": timezone.now().isoformat(),
        }
        record_file = options["record_file"]
        if record_file:
            path = Path(record_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")

        self.stdout.write(json.dumps(record, sort_keys=True))

        authenticated = False
        if run.auth_method == AccountCredential.AuthMethod.TWITCH_TV:
            tv_mode = os.environ.get("TWITCH_FARM_FAKE_TV_AUTH_MODE", "success")
            if run.reset_session:
                emit_control_event(
                    "device_code",
                    user_code="FAKE-CODE",
                    verification_uri="https://www.twitch.tv/activate",
                    expires_in=(1 if tv_mode == "expired" else 1800),
                )
            if tv_mode == "failed":
                emit_control_event("authentication_failed", error="Fake Twitch authentication failed.")
                raise SystemExit(18)
            if tv_mode == "expired":
                time.sleep(1.05)
                emit_control_event("authentication_failed", error="Fake Twitch activation code expired.")
                raise SystemExit(19)
            if tv_mode != "pending":
                emit_control_event("authenticated")
                authenticated = True
        else:
            emit_control_event("authenticated")
            authenticated = True

        if authenticated:
            emit_control_event("watching_channels", channels=channels[:2])

        exit_code = options["exit_code"]
        if mode == "exit-immediately":
            raise SystemExit(exit_code)

        if mode == "ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        if mode == "crash-after":
            time.sleep(max(0.0, options["crash_after"]))
            raise SystemExit(exit_code)

        deadline = time.monotonic() + max(0.0, options["duration"])
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
