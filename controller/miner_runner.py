"""Run one Twitch miner from an immutable database launch snapshot.

The supervisor deliberately gives this process only a ``MinerRun`` primary key
and an account configuration key.  Channels come from the immutable run row and
the Twitch password is loaded directly from ``config.yaml`` in this child.  In
particular, credentials never appear in process arguments or in SQLite.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LaunchPayload:
    """The small, in-memory-only launch specification consumed by the miner."""

    username: str
    password: str
    channels: tuple[str, ...]


def _credential_value(credentials: Any, name: str) -> str:
    """Read a credential field without imposing a config dataclass shape."""

    if isinstance(credentials, dict):
        value = credentials.get(name)
    else:
        value = getattr(credentials, name, None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Twitch account has no {name} in config.yaml.")
    return value


def load_launch_payload(run_id: int, account_key: str) -> LaunchPayload:
    """Load and validate a run snapshot plus its YAML-only credentials."""

    from controller.config import get_account_credentials
    from controller.models import MinerRun

    run = (
        MinerRun.objects.select_related("account")
        .filter(
            pk=run_id,
            account__config_key=account_key,
            ended_at__isnull=True,
        )
        .first()
    )
    if run is None:
        raise ValueError("Launch snapshot is missing, closed, or belongs to another account.")

    channels = tuple(run.channels or ())
    if not channels or any(not isinstance(channel, str) or not channel for channel in channels):
        raise ValueError("Launch snapshot has no valid channels.")

    credentials = get_account_credentials(account_key)
    return LaunchPayload(
        username=_credential_value(credentials, "username"),
        password=_credential_value(credentials, "password"),
        channels=channels,
    )


def prepare_runtime_cookie(username: str) -> Path:
    """Seed one writable runtime cookie without modifying the backup mount."""

    from django.conf import settings

    filename = f"{username}.pkl"
    if Path(filename).name != filename:
        raise ValueError("Twitch username cannot be used as a safe cookie filename.")

    runtime_cookie_dir = Path.cwd() / "cookies"
    runtime_cookie_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_cookie_dir.chmod(0o700)
    destination = runtime_cookie_dir / filename
    if destination.is_symlink():
        raise ValueError("Runtime cookie path cannot be a symbolic link.")
    if destination.exists():
        destination.chmod(0o600)
        return destination

    seed_dir = Path(
        getattr(settings, "TWITCH_FARM_COOKIES_DIR", settings.BASE_DIR / "cookies")
    )
    seed = seed_dir / filename
    if seed.is_file() and not seed.is_symlink():
        temporary = runtime_cookie_dir / f".{filename}.{os.getpid()}.tmp"
        try:
            shutil.copyfile(seed, temporary)
            temporary.chmod(0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def run_miner(payload: LaunchPayload) -> None:
    """Construct the upstream miner with the legacy behavior and start mining."""

    # Keep these imports inside the real execution path.  Management checks and
    # supervisor tests do not need to import the comparatively heavy miner stack.
    from colorama import Fore
    from TwitchChannelPointsMiner import TwitchChannelPointsMiner
    from TwitchChannelPointsMiner.classes.Chat import ChatPresence
    from TwitchChannelPointsMiner.classes.Settings import Priority
    from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
    from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings

    twitch_miner = TwitchChannelPointsMiner(
        username=payload.username,
        password=payload.password,
        claim_drops_startup=False,
        priority=[Priority.DROPS, Priority.ORDER, Priority.STREAK],
        enable_analytics=False,
        disable_ssl_cert_verification=False,
        disable_at_in_nickname=False,
        logger_settings=LoggerSettings(
            save=False,
            console_level=logging.INFO,
            console_username=True,
            auto_clear=True,
            time_zone="",
            file_level=logging.WARNING,
            emoji=True,
            less=True,
            colored=True,
            color_palette=ColorPalette(
                STREAMER_online="GREEN",
                streamer_offline="red",
                BET_wiN=Fore.MAGENTA,
            ),
        ),
        streamer_settings=StreamerSettings(
            make_predictions=False,
            follow_raid=False,
            claim_drops=True,
            claim_moments=True,
            watch_streak=True,
            chat=ChatPresence.ONLINE,
        ),
    )
    twitch_miner.mine(list(payload.channels), followers=False)


def main(run_id: int, account_key: str) -> None:
    """Initialize Django, load the exact launch snapshot, and run the miner."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twitch_farm.settings")
    import django

    django.setup()
    # This process is dedicated to one miner, so a restrictive process-wide
    # umask safely protects any cookie the upstream library creates later.
    os.umask(0o077)
    payload = load_launch_payload(run_id, account_key)
    prepare_runtime_cookie(payload.username)
    run_miner(payload)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m controller.miner_runner <run_id> <account_key>")
        raise SystemExit(2)
    main(int(sys.argv[1]), sys.argv[2])
