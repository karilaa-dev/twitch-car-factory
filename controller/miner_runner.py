"""Run one Twitch miner from an immutable database launch snapshot.

The supervisor gives this process database identifiers only.  Credentials are
decrypted in this dedicated child and never appear in argv or launch records.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
from pathlib import Path
import pickle
import signal
import sys
import threading
from dataclasses import dataclass, field

from django.views.decorators.debug import sensitive_variables


CONTROL_EVENT_PREFIX = "@@TWITCH_FARM_EVENT@@"


def emit_control_event(event: str, **payload: object) -> None:
    """Emit a single machine-only event without ever serializing credentials."""

    message = {"event": event, **payload}
    print(CONTROL_EVENT_PREFIX + json.dumps(message, separators=(",", ":")), flush=True)


def prepare_upstream_logging() -> None:
    """Remove inherited handlers before configuring the child log pipeline."""

    logging.disable(logging.DEBUG)
    root_logger = logging.getLogger()
    for handler in tuple(root_logger.handlers):
        root_logger.removeHandler(handler)


@dataclass(frozen=True, slots=True)
class LaunchPayload:
    """The small, in-memory-only launch specification consumed by the miner."""

    username: str
    password: str = field(repr=False)
    channels: tuple[str, ...]
    auth_method: str = "legacy_password"


def configure_linux_parent_death_signal() -> None:
    """Kill this miner if its process-owning supervisor disappears.

    This runs after ``exec`` in the dedicated child instead of through
    ``subprocess.Popen(preexec_fn=...)``. The supervisor drains miner output in
    background threads, and Python code in a pre-exec child can deadlock when
    the parent process is multithreaded.
    """

    parent_pid_value = os.environ.pop("TWITCH_FARM_SUPERVISOR_PID", None)
    if not sys.platform.startswith("linux") or parent_pid_value is None:
        return
    try:
        parent_pid = int(parent_pid_value)
    except ValueError as exc:
        raise RuntimeError("Supervisor PID must be a positive integer.") from exc
    if parent_pid <= 0:
        raise RuntimeError("Supervisor PID must be a positive integer.")

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL) != 0:  # PR_SET_PDEATHSIG = 1
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    # Close the race where the supervisor exits after Popen but before prctl.
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


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
        auth_method=run.auth_method,
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
    """Run the owned headless drops engine inside the supervised child."""
    from controller.twitch_miner.auth import authenticate, AuthenticationError as LoginError
    from controller.twitch_miner.client import TwitchClient, AuthenticationError
    from controller.twitch_miner.engine import DropsMiner

    prepare_upstream_logging()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = threading.Event()
    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, lambda *_args: stopped.set())
    try:
        session = authenticate(
            payload.username, payload.auth_method,
            Path.cwd() / "cookies" / f"{payload.username}.pkl",
            emit_control_event, stopped,
        )
        if stopped.is_set():
            return
        emit_control_event("authenticated")
        client = TwitchClient(token=session.token, client_id=session.client_id, user_id=session.user_id, stopped=stopped)
        DropsMiner(client, payload.username, payload.channels, emit_control_event, stopped).run()
    except (LoginError, AuthenticationError) as exc:
        if not stopped.is_set():
            emit_control_event("authentication_failed", error=str(exc))
            raise
    finally:
        emit_control_event("watching_channels", channels=[], online_channels=[])
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


@sensitive_variables()
def main(run_id: int, account_id: int) -> None:
    """Initialize Django, load the exact launch snapshot, and run the miner."""

    configure_linux_parent_death_signal()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twitch_farm.settings")
    import django

    django.setup()
    # This process is dedicated to one miner, so a restrictive process-wide
    # umask safely protects any cookie the session writer creates later.
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
