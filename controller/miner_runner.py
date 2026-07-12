"""Run one Twitch miner from an immutable database launch snapshot.

The supervisor gives this process database identifiers only.  Credentials are
decrypted in this dedicated child and never appear in argv or launch records.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import pickle
import sys
from dataclasses import dataclass, field

from django.views.decorators.debug import sensitive_variables


@dataclass(frozen=True, slots=True)
class LaunchPayload:
    """The small, in-memory-only launch specification consumed by the miner."""

    username: str
    password: str = field(repr=False)
    channels: tuple[str, ...]


@sensitive_variables()
def load_launch_payload(run_id: int, account_id: int) -> LaunchPayload:
    """Load and validate a run snapshot plus its encrypted DB credential."""

    from controller.models import MinerRun
    from controller.services import get_account_password

    run = (
        MinerRun.objects.select_related("account")
        .filter(
            pk=run_id,
            account_id=account_id,
            ended_at__isnull=True,
        )
        .first()
    )
    if run is None:
        raise ValueError("Launch snapshot is missing, closed, or belongs to another account.")

    channels = tuple(run.channels or ())
    if not channels or any(not isinstance(channel, str) or not channel for channel in channels):
        raise ValueError("Launch snapshot has no valid channels.")

    password = get_account_password(run.account)
    return LaunchPayload(
        username=run.account.display_username,
        password=password,
        channels=channels,
    )


@sensitive_variables()
def _validated_seed_payload(value) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError("Stored session seed has an invalid cookie list.")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    value_bytes = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "value"}:
            raise ValueError("Stored session seed has an invalid cookie entry.")
        name = item.get("name")
        cookie_value = item.get("value")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 256
            or any(character in name for character in "\x00\r\n;")
        ):
            raise ValueError("Stored session seed has an invalid cookie name.")
        if not isinstance(cookie_value, str) or len(cookie_value) > 16 * 1024:
            raise ValueError("Stored session seed has an invalid cookie value.")
        folded_name = name.casefold()
        if folded_name in seen:
            raise ValueError("Stored session seed contains duplicate cookie names.")
        seen.add(folded_name)
        value_bytes += len(cookie_value.encode("utf-8"))
        if value_bytes > 64 * 1024:
            raise ValueError("Stored session seed cookie values exceed the safety limit.")
        normalized.append({"name": name, "value": cookie_value})
    if not any(
        item["name"].casefold() == "auth-token" and item["value"]
        for item in normalized
    ):
        raise ValueError("Stored session seed has no authentication token.")
    return normalized


@sensitive_variables()
def prepare_runtime_cookie(username: str, account_id: int | None = None) -> Path:
    """Consume one encrypted session seed into the worker-only runtime directory."""

    from controller.crypto import decrypt_json
    from controller.models import AccountSessionSeed

    filename = f"{username}.pkl"
    if Path(filename).name != filename:
        raise ValueError("Twitch username cannot be used as a safe cookie filename.")

    runtime_cookie_dir = Path.cwd() / "cookies"
    if runtime_cookie_dir.is_symlink():
        raise ValueError("Runtime cookie directory cannot be a symbolic link.")
    runtime_cookie_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_cookie_dir.chmod(0o700)
    destination = runtime_cookie_dir / filename
    if destination.is_symlink():
        raise ValueError("Runtime cookie path cannot be a symbolic link.")

    seed = (
        AccountSessionSeed.objects.filter(account_id=account_id).first()
        if account_id is not None
        else None
    )
    if seed is None and destination.exists():
        destination.chmod(0o600)
        return destination

    if seed is not None:
        cookies = _validated_seed_payload(decrypt_json(seed.payload_ciphertext))
        temporary = runtime_cookie_dir / f".{filename}.{os.getpid()}.tmp"
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                pickle.dump(cookies, stream, protocol=4)
            os.replace(temporary, destination)
            destination.chmod(0o600)
            seed.delete()
        finally:
            temporary.unlink(missing_ok=True)
    return destination


@sensitive_variables()
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


@sensitive_variables()
def main(run_id: int, account_id: int) -> None:
    """Initialize Django, load the exact launch snapshot, and run the miner."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twitch_farm.settings")
    import django

    django.setup()
    # This process is dedicated to one miner, so a restrictive process-wide
    # umask safely protects any cookie the upstream library creates later.
    os.umask(0o077)
    payload = load_launch_payload(run_id, account_id)
    prepare_runtime_cookie(payload.username, account_id)
    try:
        run_miner(payload)
    except Exception:
        # Upstream exception messages are not trusted: an authentication
        # library could include the submitted password in its text.
        raise RuntimeError(
            "Miner execution failed; sensitive authentication details were suppressed."
        ) from None


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m controller.miner_runner <run_id> <account_id>")
        raise SystemExit(2)
    main(int(sys.argv[1]), int(sys.argv[2]))
