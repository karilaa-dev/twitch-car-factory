"""Reliable single-owner supervisor for per-account Twitch miner processes.

Web requests only describe desired state and enqueue :class:`MinerCommand`
records.  This module is the sole process owner.  Its reconciliation loop makes
observed state converge on durable desired state, records every unexpected exit,
and keeps retrying without turning a broken miner into a tight restart loop.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, IO, Protocol
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from controller.models import (
    AccountCredential,
    AccountSessionSeed,
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    RestartAttempt,
    WorkerLease,
)
from controller import services
from controller.runtime_logs import AccountRunLogWriter, recover_account_log_archives
from controller.miner_runner import CONTROL_EVENT_PREFIX


logger = logging.getLogger(__name__)
miner_output_logger = logging.getLogger("twitch_farm.miner_output")

_UPSTREAM_DEBUG_LINE = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s+(?:TRACE|DEBUG)\s+\S+:"
    r"|\d{2}/\d{2}(?:/\d{2})?\s+\d{2}:\d{2}:\d{2}\s+-\s+(?:TRACE|DEBUG)\s+-"
    r")"
)


class ProcessLike(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class SupervisorAlreadyRunning(RuntimeError):
    """Raised when either the host lock or live DB lease is already owned."""


class SupervisorLeaseLost(RuntimeError):
    """Raised when this process no longer owns the database heartbeat row."""


def _number_setting(env_name: str, setting_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        raw = getattr(settings, setting_name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{env_name} must be a number.") from exc
    if value < 0:
        raise ValueError(f"{env_name} cannot be negative.")
    return value


def _backoff_setting() -> tuple[float, ...]:
    raw = os.environ.get(
        "MINER_RAPID_RESTART_BACKOFF",
        getattr(settings, "MINER_RAPID_RESTART_BACKOFF", "5,15,30,60,120"),
    )
    if isinstance(raw, (tuple, list)):
        values = tuple(float(value) for value in raw)
    else:
        try:
            values = tuple(float(value.strip()) for value in str(raw).split(",") if value.strip())
        except ValueError as exc:
            raise ValueError("MINER_RAPID_RESTART_BACKOFF must contain numbers.") from exc
    if not values or any(value < 0 for value in values):
        raise ValueError("MINER_RAPID_RESTART_BACKOFF must contain non-negative delays.")
    return values


@dataclass(frozen=True, slots=True)
class SupervisorOptions:
    """Timing and runtime paths, injectable so reliability tests stay fast."""

    command_poll_seconds: float = 1.0
    health_poll_seconds: float = 5.0
    fingerprint_poll_seconds: float = 30.0
    startup_grace_seconds: float = 15.0
    authentication_handshake_seconds: float = 1900.0
    stop_timeout_seconds: float = 10.0
    rapid_restart_backoff: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0)
    degraded_retry_seconds: float = 300.0
    stable_reset_seconds: float = 600.0
    command_lease_seconds: float = 45.0
    worker_lease_seconds: float = 20.0
    worker_heartbeat_seconds: float = 5.0
    require_authentication_events: bool = True
    fake_miner: bool = False
    lock_path: Path | None = None

    @classmethod
    def from_settings(cls, *, command_poll_seconds: float | None = None) -> "SupervisorOptions":
        database_name = Path(settings.DATABASES["default"]["NAME"])
        lock_value = os.environ.get("TWITCH_FARM_WORKER_LOCK") or getattr(
            settings,
            "TWITCH_FARM_WORKER_LOCK",
            str(database_name.with_suffix(database_name.suffix + ".worker.lock")),
        )
        fake_raw = os.environ.get(
            "TWITCH_FARM_FAKE_MINER",
            str(getattr(settings, "TWITCH_FARM_FAKE_MINER", "0")),
        )
        return cls(
            command_poll_seconds=(
                command_poll_seconds
                if command_poll_seconds is not None
                else _number_setting("MINER_COMMAND_POLL_SECONDS", "MINER_COMMAND_POLL_SECONDS", 1)
            ),
            health_poll_seconds=_number_setting(
                "MINER_HEALTH_POLL_SECONDS", "MINER_HEALTH_POLL_SECONDS", 5
            ),
            fingerprint_poll_seconds=_number_setting(
                "MINER_FINGERPRINT_POLL_SECONDS", "MINER_FINGERPRINT_POLL_SECONDS", 30
            ),
            startup_grace_seconds=_number_setting(
                "MINER_STARTUP_GRACE_SECONDS", "MINER_STARTUP_GRACE_SECONDS", 15
            ),
            authentication_handshake_seconds=_number_setting(
                "MINER_AUTHENTICATION_HANDSHAKE_SECONDS",
                "MINER_AUTHENTICATION_HANDSHAKE_SECONDS",
                1900,
            ),
            stop_timeout_seconds=_number_setting(
                "MINER_STOP_TIMEOUT_SECONDS", "MINER_STOP_TIMEOUT_SECONDS", 10
            ),
            rapid_restart_backoff=_backoff_setting(),
            degraded_retry_seconds=_number_setting(
                "MINER_DEGRADED_RETRY_SECONDS", "MINER_DEGRADED_RETRY_SECONDS", 300
            ),
            stable_reset_seconds=_number_setting(
                "MINER_STABLE_RESET_SECONDS", "MINER_STABLE_RESET_SECONDS", 600
            ),
            command_lease_seconds=_number_setting(
                "MINER_COMMAND_LEASE_SECONDS", "MINER_COMMAND_LEASE_SECONDS", 45
            ),
            worker_lease_seconds=_number_setting(
                "MINER_WORKER_LEASE_SECONDS", "MINER_WORKER_LEASE_SECONDS", 20
            ),
            worker_heartbeat_seconds=_number_setting(
                "MINER_WORKER_HEARTBEAT_SECONDS", "MINER_WORKER_HEARTBEAT_SECONDS", 5
            ),
            require_authentication_events=True,
            fake_miner=str(fake_raw).strip().lower() in {"1", "true", "yes", "on"},
            lock_path=Path(lock_value),
        )


@dataclass(slots=True)
class ManagedProcess:
    process: ProcessLike
    account_id: int
    account_key: str
    run_id: int
    spawned_monotonic: float
    confirmed: bool = False
    command_id: int | None = None
    restart_attempt_id: int | None = None
    cleanup_required: bool = False
    cleanup_error: str = ""
    pending_stop_reason: str = ""
    pending_stop_forced: bool = False
    output_thread: threading.Thread | None = None
    log_writer: AccountRunLogWriter | None = None
    crash_logged: bool = False
    auth_required: bool = False
    authenticated: bool = False
    authentication_deadline: float | None = None
    control_events: queue.SimpleQueue[dict[str, object]] = dataclass_field(
        default_factory=queue.SimpleQueue
    )
    latest_watching_event: dict[str, object] | None = None
    control_event_lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def put_control_event(self, event: dict[str, object]) -> None:
        if event.get("event") == "watching_channels":
            with self.control_event_lock:
                self.latest_watching_event = event
            return
        self.control_events.put(event)

    def pop_watching_event(self) -> dict[str, object] | None:
        with self.control_event_lock:
            event = self.latest_watching_event
            self.latest_watching_event = None
            return event


_SENSITIVE_VALUE = re.compile(
    r"""(?ix)
    (?P<label>
        \b(?:
            password|passwd|secret|token|
            api[_-]?key|access[_-]?token|refresh[_-]?token|
            auth[_-]?token|client[_-]?secret
        )\b
        ["']?\s*[:=]\s*
    )
    (?:
        "(?:\\.|[^"\\])*" |
        '(?:\\.|[^'\\])*' |
        (?:bearer|basic)\s+[^\s,;}\]]+ |
        [^\s,;}\]]+
    )
    """
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?im)(?P<label>\bauthorization\b[\"']?\s*[:=]\s*)[^\r\n]*"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def safe_error(value: object, *, limit: int = 2000) -> str:
    """Bound and redact errors before persisting them or showing them in HTML."""

    text = str(value).replace("\x00", "")
    text = _AUTHORIZATION_VALUE.sub(r"\g<label>[redacted]", text)
    text = _SENSITIVE_VALUE.sub(r"\g<label>[redacted]", text)
    return text[:limit]


def _clear_watching_state(state: MinerInstanceState) -> None:
    state.watching_channels = []
    state.watching_updated_at = None


def _parse_control_event(line: str) -> dict[str, object] | None:
    if not line.startswith(CONTROL_EVENT_PREFIX):
        return None
    payload = line[len(CONTROL_EVENT_PREFIX) :]
    if len(payload) > 2048:
        raise ValueError("Control event exceeded the size limit.")
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Control event was not valid JSON.") from exc
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        raise ValueError("Control event has an invalid shape.")
    forbidden = {"token", "device_code", "access_token", "refresh_token", "password"}
    if forbidden.intersection(str(key).casefold() for key in event):
        raise ValueError("Control event contained a forbidden secret field.")
    kind = event["event"]
    if kind == "device_code":
        if set(event) != {"event", "user_code", "verification_uri", "expires_in"}:
            raise ValueError("Device-code event has unexpected fields.")
        code = event["user_code"]
        uri = event["verification_uri"]
        expires_in = event["expires_in"]
        parsed = urlparse(uri if isinstance(uri, str) else "")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9-]{4,32}", code):
            raise ValueError("Device-code event has an invalid user code.")
        if parsed.scheme != "https" or parsed.hostname != "www.twitch.tv" or parsed.path != "/activate":
            raise ValueError("Device-code event has an invalid activation URL.")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or not 1 <= expires_in <= 3600:
            raise ValueError("Device-code event has an invalid expiry.")
    elif kind == "authenticated":
        if set(event) != {"event"}:
            raise ValueError("Authenticated event has unexpected fields.")
    elif kind == "authentication_failed":
        if set(event) != {"event", "error"} or not isinstance(event.get("error"), str):
            raise ValueError("Authentication-failure event has an invalid shape.")
        event["error"] = safe_error(event["error"], limit=500)
    elif kind == "watching_channels":
        if set(event) != {"event", "channels"} or not isinstance(event.get("channels"), list):
            raise ValueError("Watching-channels event has an invalid shape.")
        channels = event["channels"]
        if len(channels) > 2:
            raise ValueError("Watching-channels event exceeded the channel limit.")
        try:
            normalized = services.normalize_channels(channels, require_nonempty=False)
        except ValidationError as exc:
            raise ValueError("Watching-channels event has invalid channel names.") from exc
        if len(normalized) != len(channels):
            raise ValueError("Watching-channels event has duplicate or empty channel names.")
        event["channels"] = normalized
    else:
        raise ValueError("Control event type is unsupported.")
    return event


def _drain_miner_output(
    stream: IO[str],
    account_key: str,
    log_writer: AccountRunLogWriter | None = None,
    event_handler: Callable[[dict[str, object]], None] | None = None,
) -> None:
    """Forward one child's output through the worker's console/file handlers."""

    try:
        for raw_line in stream:
            unstyled_line = _ANSI_ESCAPE.sub("", raw_line.rstrip("\r\n"))
            line = safe_error(unstyled_line, limit=8000)
            if line:
                if line.startswith(CONTROL_EVENT_PREFIX):
                    try:
                        event = _parse_control_event(line)
                    except ValueError as exc:
                        logger.warning("Rejected miner control event for %s: %s", account_key, exc)
                        if log_writer is not None:
                            log_writer.lifecycle("control_event_rejected", error=safe_error(exc))
                    else:
                        if event is not None and event_handler is not None:
                            event_handler(event)
                    continue
                # The pinned miner sets the root logger to DEBUG internally.
                # Keep protocol payloads out of both combined and account logs
                # even if its handler configuration regresses in a later fork.
                if _UPSTREAM_DEBUG_LINE.match(line):
                    continue
                miner_output_logger.info("miner[%s] %s", account_key, line)
                if log_writer is not None:
                    log_writer.write(line, kind="library")
    except (OSError, ValueError):
        logger.exception("Miner output stream failed for %s", account_key)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


class MinerSupervisor:
    """Own child processes and reconcile them with durable controller state."""

    lease_name = "miner-supervisor"

    def __init__(
        self,
        *,
        options: SupervisorOptions | None = None,
        process_factory: Callable[..., ProcessLike] = subprocess.Popen,
        now: Callable[[], datetime] = timezone.now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        worker_id: str | None = None,
        use_file_lock: bool = True,
    ) -> None:
        self.options = options or SupervisorOptions.from_settings()
        self.process_factory = process_factory
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.worker_id = worker_id or f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex}"
        self.use_file_lock = use_file_lock
        self.processes: dict[int, ManagedProcess] = {}
        self.pending_run_finalizations: dict[
            int, tuple[int, int | None, str, str]
        ] = {}
        self._pending_log_finalizations: dict[int, threading.Thread] = {}
        self._pending_log_finalizations_lock = threading.Lock()
        self._lock_handle: IO[str] | None = None
        self._started = False
        self._shutdown_incomplete = False
        self._last_health = float("-inf")
        self._last_fingerprint = float("-inf")
        self._last_heartbeat = float("-inf")
        self._recovered_stale_lease = False

    # -- singleton ownership -------------------------------------------------

    def _acquire_file_lock(self) -> None:
        if not self.use_file_lock or self.options.lock_path is None:
            return
        path = self.options.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise SupervisorAlreadyRunning(f"Miner supervisor lock is held: {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(self.worker_id + "\n")
        handle.flush()
        self._lock_handle = handle

    def _release_file_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None

    def _acquire_database_lease(self) -> None:
        now = self.now()
        expires_at = now + timedelta(seconds=self.options.worker_lease_seconds)
        with transaction.atomic():
            lease = WorkerLease.objects.select_for_update().filter(name=self.lease_name).first()
            if lease is not None and lease.owner_id != self.worker_id and lease.expires_at > now:
                raise SupervisorAlreadyRunning(
                    f"Miner supervisor DB lease belongs to {lease.owner_id}."
                )
            stale_owner = lease.owner_id if lease is not None else ""
            stale_expires_at = lease.expires_at if lease is not None else now
            stale = lease is not None and stale_owner != self.worker_id and lease.expires_at <= now
            if lease is None:
                lease = WorkerLease(name=self.lease_name, owner_id=self.worker_id)
            lease.owner_id = self.worker_id
            lease.pid = os.getpid()
            lease.acquired_at = now
            lease.heartbeat_at = now
            lease.expires_at = expires_at
            lease.save()

            if stale:
                MinerIncident.objects.create(
                    kind=MinerIncident.Kind.UNCLEAN_SUPERVISOR,
                    status=MinerIncident.Status.RECOVERED,
                    summary="Previous miner supervisor stopped without releasing its lease.",
                    details=f"A stale worker lease was recovered by {self.worker_id}.",
                    opened_at=stale_expires_at,
                    recovered_at=now,
                )
                self._recovered_stale_lease = True

    def heartbeat(self, *, force: bool = False) -> None:
        current = self.monotonic()
        if not force and current - self._last_heartbeat < self.options.worker_heartbeat_seconds:
            return
        now = self.now()
        expires_at = now + timedelta(seconds=self.options.worker_lease_seconds)
        updated = WorkerLease.objects.filter(
            name=self.lease_name,
            owner_id=self.worker_id,
        ).update(
            pid=os.getpid(),
            heartbeat_at=now,
            expires_at=expires_at,
        )
        if updated != 1:
            raise SupervisorLeaseLost("Miner supervisor lost its database lease.")

        run_ids = [
            managed.run_id
            for managed in self.processes.values()
            if not managed.cleanup_required and managed.process.poll() is None
        ]
        if run_ids:
            MinerInstanceState.objects.filter(
                worker_id=self.worker_id,
                current_run_id__in=run_ids,
            ).update(last_heartbeat=now)
        command_ids = [
            managed.command_id for managed in self.processes.values() if managed.command_id
        ]
        if command_ids:
            MinerCommand.objects.filter(
                pk__in=command_ids,
                status=MinerCommand.Status.LEASED,
                lease_owner=self.worker_id,
            ).update(lease_expires_at=now + timedelta(seconds=self._command_lease_duration()))
        self._last_heartbeat = current

    def _command_lease_duration(self) -> float:
        return max(
            self.options.command_lease_seconds,
            self.options.startup_grace_seconds + self.options.worker_heartbeat_seconds + 1,
        )

    # -- startup reconciliation ---------------------------------------------

    def startup(self) -> None:
        if self._started:
            return
        self._acquire_file_lock()
        try:
            self._acquire_database_lease()
            services.purge_expired_legacy_import_drafts(at=self.now())
            self.reconcile_startup()
            if getattr(settings, "TWITCH_FARM_LOG_WRITER", False):
                try:
                    recovery_errors = recover_account_log_archives()
                except Exception:
                    logger.exception("Account log archive recovery failed during startup")
                else:
                    for recovery_error in recovery_errors:
                        logger.error("Account log recovery is pending: %s", recovery_error)
            self.heartbeat(force=True)
            self._started = True
            # Recovered queued admin intent wins before automatic convergence.
            # In particular, don't spawn a startup recovery child only to have
            # an already-durable restart command replace it immediately.
            self.process_pending_commands()
            self.reconcile_desired_state()
            # Accounts with an open crash incident are deliberately skipped by
            # direct desired-state reconciliation. Start their newly recorded
            # recovery attempt now so a worker restart preserves the same
            # incident/audit chain instead of silently bypassing it.
            self._perform_due_recoveries()
            logger.info("Miner supervisor online: worker=%s pid=%s", self.worker_id, os.getpid())
        except Exception:
            WorkerLease.objects.filter(
                name=self.lease_name,
                owner_id=self.worker_id,
            ).delete()
            self._release_file_lock()
            raise

    def reconcile_startup(self) -> None:
        """Discard advisory PIDs, expire dead claims, and preserve desired state."""

        now = self.now()
        # Once this worker owns both singleton locks, every command claim from a
        # different worker is necessarily orphaned even if its timestamp has not
        # expired yet.  Recover it immediately instead of delaying convergence.
        MinerCommand.objects.filter(status=MinerCommand.Status.LEASED).exclude(
            lease_owner=self.worker_id
        ).update(
            status=MinerCommand.Status.QUEUED,
            lease_owner="",
            lease_expires_at=None,
            leased_at=None,
            error="",
        )

        lost_process_ownership = False
        for state in MinerInstanceState.objects.select_related("current_run", "account"):
            if state.current_run_id and state.current_run and state.current_run.ended_at is None:
                lost_process_ownership = True
                MinerRun.objects.filter(pk=state.current_run_id, ended_at__isnull=True).update(
                    ended_at=now,
                    stop_reason=MinerRun.StopReason.UNEXPECTED_EXIT,
                    error="Process ownership was lost before supervisor reconciliation.",
                )
            state.current_run = None
            state.advisory_pid = None
            state.worker_id = ""
            state.last_heartbeat = None
            state.stable_since = None
            state.next_retry_at = None
            if state.authentication_status == MinerInstanceState.AuthenticationStatus.PENDING:
                state.authentication_status = MinerInstanceState.AuthenticationStatus.UNLINKED
                state.authentication_uri = ""
                state.authentication_code = ""
                state.authentication_expires_at = None
                state.authentication_error = ""
                state.authentication_updated_at = now
            state.observed_state = (
                MinerInstanceState.ObservedState.UNKNOWN
                if state.desired_state == MinerInstanceState.DesiredState.RUNNING
                else MinerInstanceState.ObservedState.STOPPED
            )
            state.save(
                update_fields=(
                    "current_run",
                    "advisory_pid",
                    "worker_id",
                    "last_heartbeat",
                    "stable_since",
                    "next_retry_at",
                    "authentication_status",
                    "authentication_uri",
                    "authentication_code",
                    "authentication_expires_at",
                    "authentication_error",
                    "authentication_updated_at",
                    "observed_state",
                    "updated_at",
                )
            )
        # Also close launch snapshots that were created but never attached to a
        # state row (for example, a runtime-directory/Popen failure followed by
        # a database write failure). A fresh singleton owns no prior child.
        unconfirmed_orphans = MinerRun.objects.filter(
            ended_at__isnull=True,
            startup_confirmed_at__isnull=True,
        )
        confirmed_orphans = MinerRun.objects.filter(
            ended_at__isnull=True,
            startup_confirmed_at__isnull=False,
        )
        orphaned_count = unconfirmed_orphans.count() + confirmed_orphans.count()
        if orphaned_count:
            lost_process_ownership = True
            unconfirmed_orphans.update(
                ended_at=now,
                stop_reason=MinerRun.StopReason.START_FAILED,
                error="Unattached launch was finalized during supervisor startup.",
            )
            confirmed_orphans.update(
                ended_at=now,
                stop_reason=MinerRun.StopReason.UNEXPECTED_EXIT,
                error="Confirmed process ownership was lost before supervisor startup.",
            )
        # A worker can die after marking a recovery attempt STARTED but before
        # it can report the result. The new singleton owns no process from that
        # attempt, so leaving it STARTED would be false telemetry forever.
        RestartAttempt.objects.filter(
            outcome__in=(
                RestartAttempt.Outcome.SCHEDULED,
                RestartAttempt.Outcome.STARTED,
            )
        ).update(
            outcome=RestartAttempt.Outcome.FAILED,
            finished_at=now,
            error="Recovery attempt was superseded by supervisor startup reconciliation.",
        )
        for state in MinerInstanceState.objects.select_related("account").filter(
            desired_state=MinerInstanceState.DesiredState.RUNNING,
        ):
            incident = MinerIncident.objects.filter(
                account=state.account,
                status=MinerIncident.Status.OPEN,
            ).first()
            if incident is not None:
                self._schedule_recovery(state, incident, immediate=True)
        if lost_process_ownership and not self._recovered_stale_lease:
            MinerIncident.objects.create(
                kind=MinerIncident.Kind.UNCLEAN_SUPERVISOR,
                status=MinerIncident.Status.RECOVERED,
                summary="Stale miner process ownership was reconciled.",
                details="Persisted process IDs were discarded and desired state was restored.",
                opened_at=now,
                recovered_at=now,
            )

    # -- command queue -------------------------------------------------------

    def _lease_next_command(self) -> MinerCommand | None:
        now = self.now()
        with transaction.atomic():
            command = (
                MinerCommand.objects.select_for_update()
                .select_related("account")
                .filter(status=MinerCommand.Status.QUEUED)
                .order_by("created_at", "id")
                .first()
            )
            if command is None:
                return None
            command.status = MinerCommand.Status.LEASED
            command.lease_owner = self.worker_id
            command.leased_at = now
            command.lease_expires_at = now + timedelta(seconds=self._command_lease_duration())
            command.attempts = F("attempts") + 1
            command.error = ""
            command.save(
                update_fields=(
                    "status",
                    "lease_owner",
                    "leased_at",
                    "lease_expires_at",
                    "attempts",
                    "error",
                    "updated_at",
                )
            )
        return MinerCommand.objects.select_related("account").get(pk=command.pk)

    def _finish_command(self, command_id: int, status: str, error: object = "") -> None:
        now = self.now()
        MinerCommand.objects.filter(
            pk=command_id,
            status=MinerCommand.Status.LEASED,
            lease_owner=self.worker_id,
        ).update(
            status=status,
            completed_at=now,
            lease_owner="",
            lease_expires_at=None,
            error=safe_error(error) if error else "",
        )

    def _recover_incident_for_healthy_fallback(self, account_id: int) -> bool:
        """Close an incident when all restart commands are terminal and the old child is healthy."""

        managed = self.processes.get(account_id)
        if (
            managed is None
            or not managed.confirmed
            or managed.cleanup_required
            or managed.pending_stop_reason
            or managed.process.poll() is not None
        ):
            return False

        with transaction.atomic():
            state = MinerInstanceState.objects.select_for_update().get(account_id=account_id)
            if (
                state.desired_state != MinerInstanceState.DesiredState.RUNNING
                or state.current_run_id != managed.run_id
                or MinerCommand.objects.filter(
                    account_id=account_id,
                    action=MinerCommand.Action.RESTART,
                    status__in=(
                        MinerCommand.Status.QUEUED,
                        MinerCommand.Status.LEASED,
                    ),
                ).exists()
            ):
                return False
            return (
                MinerIncident.objects.filter(
                    account_id=account_id,
                    status=MinerIncident.Status.OPEN,
                ).update(
                    status=MinerIncident.Status.RECOVERED,
                    recovered_at=self.now(),
                )
                > 0
            )

    def _is_superseded(self, command: MinerCommand) -> bool:
        return MinerCommand.objects.filter(
            account_id=command.account_id,
            status__in=(MinerCommand.Status.QUEUED, MinerCommand.Status.LEASED),
        ).filter(
            Q(created_at__gt=command.created_at)
            | Q(created_at=command.created_at, id__gt=command.id)
        ).exists()

    def execute_command(self, command: MinerCommand) -> bool:
        """Execute one leased command; return true while startup confirmation is pending."""

        if self._is_superseded(command):
            self._finish_command(
                command.pk,
                MinerCommand.Status.CANCELLED,
                "Superseded by a newer command for this account.",
            )
            if command.action == MinerCommand.Action.RESTART:
                self._recover_incident_for_healthy_fallback(command.account_id)
            return False

        state, _ = MinerInstanceState.objects.get_or_create(account=command.account)
        expected_desired = (
            MinerInstanceState.DesiredState.STOPPED
            if command.action == MinerCommand.Action.STOP
            else MinerInstanceState.DesiredState.RUNNING
        )
        # Desired state is written transactionally when the command is queued.
        # The process owner must never rewrite it: doing so lets an older leased
        # command overwrite a newer admin request that arrived concurrently.
        if state.desired_state != expected_desired:
            self._finish_command(
                command.pk,
                MinerCommand.Status.CANCELLED,
                "Desired state changed after this command was queued.",
            )
            if command.action == MinerCommand.Action.RESTART:
                self._recover_incident_for_healthy_fallback(command.account_id)
            return False
        try:
            if command.action == MinerCommand.Action.STOP:
                self.stop_account(
                    command.account,
                    reason=MinerRun.StopReason.ADMIN_STOP,
                    preserve_desired=False,
                    except_command_id=command.pk,
                )
                self._finish_command(command.pk, MinerCommand.Status.SUCCEEDED)
                return False

            if command.action == MinerCommand.Action.START:
                managed = self.start_account(
                    command.account,
                    reset_failures=True,
                    command_id=command.pk,
                )
            elif command.action == MinerCommand.Action.RESTART:
                stop_reason = (
                    MinerRun.StopReason.CONFIG_RESTART
                    if "config" in command.reason.casefold()
                    else MinerRun.StopReason.ADMIN_RESTART
                )
                managed = self.restart_account(
                    command.account,
                    reason=stop_reason,
                    reset_failures=True,
                    command_id=command.pk,
                )
            elif command.action == MinerCommand.Action.AUTHENTICATE:
                managed = self.authenticate_account(
                    command.account,
                    command_id=command.pk,
                )
            else:
                raise ValueError(f"Unsupported miner command action: {command.action}")

            if managed.confirmed:
                self._finish_command(command.pk, MinerCommand.Status.SUCCEEDED)
                return False
            return True
        except Exception as exc:
            self._finish_command(command.pk, MinerCommand.Status.FAILED, exc)
            if command.action == MinerCommand.Action.RESTART:
                self._recover_incident_for_healthy_fallback(command.account_id)
            logger.exception("Miner command %s failed", command.pk)
            return False

    def _remove_runtime_session(self, account: MinerAccount) -> None:
        """Remove worker-owned session material without following symbolic links."""

        runtime_dir = Path(
            getattr(settings, "TWITCH_FARM_RUNTIME_DIR", settings.BASE_DIR / "runtime")
        )
        filename = f"{account.display_username}.pkl"
        if Path(filename).name != filename:
            raise ValueError("Twitch username cannot identify a safe cookie file.")
        cookie_dir = runtime_dir / "cookies"
        cookie = cookie_dir / filename
        if cookie_dir.is_symlink() or cookie.is_symlink():
            raise ValueError("Runtime cookie storage cannot use symbolic links.")
        if cookie.exists():
            cookie.unlink()
        AccountSessionSeed.objects.filter(account=account).delete()

    def authenticate_account(
        self,
        account: MinerAccount,
        *,
        command_id: int,
    ) -> ManagedProcess:
        """Stop the miner, clear its session, and launch one explicit TV handshake."""

        run = services.create_launch_snapshot(
            account,
            worker_id=self.worker_id,
            reset_session=True,
        )
        try:
            self.stop_account(
                account,
                reason=MinerRun.StopReason.AUTHENTICATION_RESET,
                preserve_desired=True,
                final_observed=MinerInstanceState.ObservedState.STARTING,
                except_command_id=command_id,
            )
            self._remove_runtime_session(account)
            state, _ = MinerInstanceState.objects.get_or_create(account=account)
            state.authentication_status = MinerInstanceState.AuthenticationStatus.UNLINKED
            state.authentication_uri = ""
            state.authentication_code = ""
            state.authentication_expires_at = None
            state.authentication_error = ""
            state.authentication_updated_at = self.now()
            state.save(
                update_fields=(
                    "authentication_status",
                    "authentication_uri",
                    "authentication_code",
                    "authentication_expires_at",
                    "authentication_error",
                    "authentication_updated_at",
                    "updated_at",
                )
            )
            return self._spawn_snapshot_if_desired_running(run, command_id=command_id)
        except Exception as exc:
            owned = self.processes.get(account.pk)
            if owned is None or owned.run_id != run.pk:
                self._finalize_or_remember(
                    run_id=run.pk,
                    account_id=run.account_id,
                    returncode=None,
                    reason=MinerRun.StopReason.AUTHENTICATION_FAILED,
                    error=exc,
                )
            raise

    def process_pending_commands(self, *, limit: int = 100) -> int:
        processed = 0
        for _ in range(limit):
            command = self._lease_next_command()
            if command is None:
                break
            self.execute_command(command)
            processed += 1
            self.heartbeat(force=True)
        return processed

    # -- process lifecycle ---------------------------------------------------

    def _write_managed_lifecycle(
        self,
        managed: ManagedProcess,
        event: str,
        **details,
    ) -> None:
        if managed.log_writer is not None:
            managed.log_writer.lifecycle(event, **details)

    def _finalize_managed_log(
        self,
        managed: ManagedProcess,
        event: str,
        **details,
    ) -> None:
        writer = managed.log_writer
        if writer is None:
            return
        thread = managed.output_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.options.stop_timeout_seconds))
            if thread.is_alive():
                logger.warning(
                    "Miner output drain is still finishing for %s; log finalization was deferred.",
                    managed.account_key,
                )
                self._defer_managed_log_finalization(
                    managed,
                    writer,
                    thread,
                    event,
                    details,
                )
                return
        self._complete_managed_log_finalization(managed, writer, event, details)

    def _complete_managed_log_finalization(
        self,
        managed: ManagedProcess,
        writer: AccountRunLogWriter,
        event: str,
        details: dict,
    ) -> None:
        if details.get("forced"):
            writer.lifecycle("forced_termination", **details)
        writer.lifecycle(event, **details)
        writer.finalize("final_exit", outcome=event, **details)
        if managed.log_writer is writer:
            managed.log_writer = None
        if writer.last_error:
            logger.error(
                "Account log finalization is pending for %s run %s: %s",
                managed.account_key,
                managed.run_id,
                writer.last_error,
            )

    def _defer_managed_log_finalization(
        self,
        managed: ManagedProcess,
        writer: AccountRunLogWriter,
        output_thread: threading.Thread,
        event: str,
        details: dict,
    ) -> None:
        run_id = managed.run_id
        with self._pending_log_finalizations_lock:
            if run_id in self._pending_log_finalizations:
                return

            def finish_after_drain() -> None:
                try:
                    output_thread.join(timeout=max(1.0, self.options.stop_timeout_seconds))
                    if output_thread.is_alive():
                        logger.error(
                            "Miner output drain did not finish for %s run %s; "
                            "log finalization was left for startup recovery.",
                            managed.account_key,
                            run_id,
                        )
                        return
                    self._complete_managed_log_finalization(
                        managed,
                        writer,
                        event,
                        details,
                    )
                except Exception:
                    logger.exception(
                        "Deferred account log finalization failed for %s run %s",
                        managed.account_key,
                        run_id,
                    )
                finally:
                    with self._pending_log_finalizations_lock:
                        self._pending_log_finalizations.pop(run_id, None)

            finalizer = threading.Thread(
                target=finish_after_drain,
                name=f"miner-log-finalize-{managed.account_id}-{run_id}",
                daemon=True,
            )
            self._pending_log_finalizations[run_id] = finalizer
            finalizer.start()

    def _wait_for_pending_log_finalizations(self) -> None:
        deadline = time.monotonic() + max(1.0, self.options.stop_timeout_seconds)
        while True:
            with self._pending_log_finalizations_lock:
                pending = list(self._pending_log_finalizations.values())
            if not pending:
                return
            for finalizer in pending:
                if finalizer is not threading.current_thread():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.error("Timed out waiting for pending account log finalizations.")
                        return
                    finalizer.join(timeout=remaining)
            if any(finalizer.is_alive() for finalizer in pending):
                if time.monotonic() >= deadline:
                    logger.error("Timed out waiting for pending account log finalizations.")
                    return

    def build_process_command(self, run: MinerRun) -> list[str]:
        """Return argv containing identifiers only, never secrets or channels."""

        if self.options.fake_miner:
            return [
                sys.executable,
                str(Path(settings.BASE_DIR) / "manage.py"),
                "run_fake_miner",
                str(run.pk),
                str(run.account_id),
            ]
        return [
            sys.executable,
            "-m",
            "controller.miner_runner",
            str(run.pk),
            str(run.account_id),
        ]

    def _terminate_owned_process(self, process: ProcessLike) -> tuple[int, bool]:
        """Terminate a child and return only after its death is confirmed.

        Callers retain the ``ManagedProcess`` entry until this method returns.
        Any signaling/wait failure therefore leaves a handle that later health
        reconciliation can retry instead of silently orphaning the child.
        """

        returncode = process.poll()
        if returncode is not None:
            return returncode, False

        forced = False
        try:
            process.terminate()
        except ProcessLookupError:
            returncode = process.poll()
            if returncode is None:
                raise RuntimeError("Process disappeared but its exit could not be confirmed.")
            return returncode, forced

        try:
            returncode = process.wait(timeout=self.options.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            forced = True
            try:
                process.kill()
            except ProcessLookupError:
                returncode = process.poll()
                if returncode is None:
                    raise RuntimeError("Process disappeared but its exit could not be confirmed.")
                return returncode, forced
            returncode = process.wait(timeout=self.options.stop_timeout_seconds)

        if returncode is None:
            raise RuntimeError("Process did not report an exit after termination.")
        return returncode, forced

    def _finalize_or_remember(
        self,
        *,
        run_id: int,
        account_id: int,
        returncode: int | None,
        reason: str,
        error: object,
    ) -> bool:
        """Finalize a run or retain an in-memory retry record until it commits."""

        bounded_error = safe_error(error)
        try:
            self._close_run(
                run_id,
                returncode=returncode,
                reason=reason,
                error=bounded_error,
            )
        except Exception as exc:
            self.pending_run_finalizations[run_id] = (
                account_id,
                returncode,
                reason,
                bounded_error,
            )
            logger.error(
                "Run %s finalization is pending after a database error: %s",
                run_id,
                safe_error(exc),
            )
            return False
        self.pending_run_finalizations.pop(run_id, None)
        return True

    def _flush_pending_run_finalizations(self, *, account_id: int | None = None) -> int:
        for run_id, pending in list(self.pending_run_finalizations.items()):
            pending_account_id, returncode, reason, error = pending
            if account_id is not None and pending_account_id != account_id:
                continue
            self._finalize_or_remember(
                run_id=run_id,
                account_id=pending_account_id,
                returncode=returncode,
                reason=reason,
                error=error,
            )
        return sum(
            pending_account_id == account_id
            for pending_account_id, _returncode, _reason, _error in self.pending_run_finalizations.values()
        ) if account_id is not None else len(self.pending_run_finalizations)

    def _spawn_snapshot(
        self,
        run: MinerRun,
        *,
        command_id: int | None = None,
        restart_attempt_id: int | None = None,
    ) -> ManagedProcess:
        log_writer: AccountRunLogWriter | None = None
        try:
            if getattr(settings, "TWITCH_FARM_LOG_WRITER", False):
                log_writer = AccountRunLogWriter(
                    account_id=run.account_id,
                    run_id=run.pk,
                    account_key=run.account.config_key,
                )
                log_writer.lifecycle(
                    "launch_requested",
                    source_mode=run.source_mode,
                    source_name=run.source_name,
                    channels=run.channels,
                    auth_method=run.auth_method,
                    reset_session=run.reset_session,
                )
                if restart_attempt_id is not None:
                    log_writer.lifecycle(
                        "recovery_started",
                        restart_attempt_id=restart_attempt_id,
                    )
            runtime_dir = Path(
                getattr(settings, "TWITCH_FARM_RUNTIME_DIR", settings.BASE_DIR / "runtime")
            )
            runtime_dir.mkdir(parents=True, exist_ok=True)
            child_environment = os.environ.copy()
            # The worker is the sole rotating-file writer. Child output is
            # captured below and re-emitted through the worker handlers.
            child_environment["TWITCH_FARM_LOG_WRITER"] = "0"
            child_environment["PYTHONUNBUFFERED"] = "1"
            existing_pythonpath = child_environment.get("PYTHONPATH", "")
            child_environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(settings.BASE_DIR), existing_pythonpath) if part
            )
            popen_options = {
                # The upstream miner hardcodes ./cookies. Running children from
                # the worker-only runtime directory keeps every refreshable
                # session outside the web service's filesystem view.
                "cwd": runtime_dir,
                "env": child_environment,
                "close_fds": True,
                "start_new_session": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if sys.platform.startswith("linux"):
                # The child installs PR_SET_PDEATHSIG at the top of its Python
                # entry point. Avoid preexec_fn here: output-draining threads
                # make running Python code between fork and exec unsafe.
                child_environment["TWITCH_FARM_SUPERVISOR_PID"] = str(os.getpid())
            process = self.process_factory(self.build_process_command(run), **popen_options)
        except Exception as exc:
            error = safe_error(exc)
            if log_writer is not None:
                log_writer.lifecycle("start_failed", error=error)
                log_writer.finalize("final_exit", outcome="start_failed", error=error)
                if log_writer.last_error:
                    logger.error(
                        "Account log finalization is pending for %s run %s: %s",
                        run.account.config_key,
                        run.pk,
                        log_writer.last_error,
                    )
            self._finalize_or_remember(
                run_id=run.pk,
                account_id=run.account_id,
                returncode=None,
                reason=MinerRun.StopReason.START_FAILED,
                error=error,
            )
            state, _ = MinerInstanceState.objects.get_or_create(account=run.account)
            state.current_run = None
            state.advisory_pid = None
            state.worker_id = ""
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = error
            state.next_retry_at = self.now() + timedelta(
                seconds=self.options.degraded_retry_seconds
            )
            state.save(
                update_fields=(
                    "current_run",
                    "advisory_pid",
                    "worker_id",
                    "observed_state",
                    "last_error",
                    "next_retry_at",
                    "updated_at",
                )
            )
            raise

        managed = ManagedProcess(
            process=process,
            account_id=run.account_id,
            account_key=run.account.config_key,
            run_id=run.pk,
            spawned_monotonic=self.monotonic(),
            command_id=command_id,
            restart_attempt_id=restart_attempt_id,
            log_writer=log_writer,
            auth_required=(
                self.options.require_authentication_events
                or run.auth_method == AccountCredential.AuthMethod.TWITCH_TV
            ),
            authentication_deadline=(
                self.monotonic() + self.options.authentication_handshake_seconds
                if (
                    self.options.require_authentication_events
                    or run.auth_method == AccountCredential.AuthMethod.TWITCH_TV
                )
                else None
            ),
        )
        # Register ownership immediately after Popen. If any following database
        # write fails, cleanup can still signal this exact child and a failed
        # cleanup remains visible to later reconciliation.
        self.processes[run.account_id] = managed
        output_stream = getattr(process, "stdout", None)
        if output_stream is not None:
            managed.output_thread = threading.Thread(
                target=_drain_miner_output,
                args=(output_stream, run.account.config_key, log_writer, managed.put_control_event),
                name=f"miner-log-{run.account_id}",
                daemon=True,
            )
            managed.output_thread.start()
        logger.info(
            "Miner process started: account=%s run=%s pid=%s",
            run.account.config_key,
            run.pk,
            process.pid,
        )
        self._write_managed_lifecycle(managed, "process_started", pid=process.pid)
        try:
            MinerRun.objects.filter(pk=run.pk).update(pid=process.pid, worker_id=self.worker_id)
            state, _ = MinerInstanceState.objects.get_or_create(account=run.account)
            state.current_run_id = run.pk
            state.advisory_pid = process.pid
            state.worker_id = self.worker_id
            state.observed_state = MinerInstanceState.ObservedState.STARTING
            state.last_heartbeat = self.now()
            state.last_error = ""
            state.next_retry_at = None
            state.stable_since = None
            _clear_watching_state(state)
            state.save(
                update_fields=(
                    "current_run",
                    "advisory_pid",
                    "worker_id",
                    "observed_state",
                    "last_heartbeat",
                    "last_error",
                    "next_retry_at",
                    "stable_since",
                    "watching_channels",
                    "watching_updated_at",
                    "updated_at",
                )
            )
        except Exception as exc:
            error = safe_error(f"Could not persist spawned miner ownership: {exc}")
            try:
                returncode, forced = self._terminate_owned_process(process)
            except Exception as cleanup_exc:
                # Keep the exact live handle registered. This prevents a second
                # child from being launched. The cleanup_required marker also
                # prevents the health loop from ever confirming this child as
                # healthy before termination and durable run finalization.
                managed.cleanup_required = True
                managed.cleanup_error = safe_error(
                    f"{error}; child cleanup failed: {cleanup_exc}"
                )
                logger.critical(
                    "Spawn persistence failed and child cleanup also failed for %s: %s",
                    run.account.config_key,
                    safe_error(cleanup_exc),
                )
            else:
                try:
                    self._close_run(
                        run.pk,
                        returncode=returncode,
                        reason=MinerRun.StopReason.START_FAILED,
                        error=(
                            f"{error} Forced kill was required."
                            if forced
                            else error
                        ),
                    )
                except Exception as finalization_exc:
                    # The process is dead but the still-registered handle is a
                    # durable-finalization tombstone. Never drop it until the
                    # run-ending write succeeds on a later health pass.
                    managed.cleanup_required = True
                    managed.cleanup_error = safe_error(
                        f"{error}; run finalization failed: {finalization_exc}"
                    )
                    logger.critical(
                        "Spawn cleanup could not finalize run %s: %s",
                        run.pk,
                        safe_error(finalization_exc),
                    )
                else:
                    if self.processes.get(run.account_id) is managed:
                        self._finalize_managed_log(
                            managed,
                            "start_failed",
                            returncode=returncode,
                            forced=forced,
                            error=error,
                        )
                        self.processes.pop(run.account_id, None)
            raise
        return managed

    def _spawn_snapshot_if_desired_running(
        self,
        run: MinerRun,
        *,
        command_id: int | None = None,
    ) -> ManagedProcess:
        """Linearize a controller launch with concurrent desired-state writes."""

        managed: ManagedProcess | None = None
        try:
            with transaction.atomic():
                state = MinerInstanceState.objects.select_for_update().get(
                    account_id=run.account_id
                )
                if state.desired_state != MinerInstanceState.DesiredState.RUNNING:
                    error = "Launch was cancelled because desired state changed before spawn."
                    self._close_run(
                        run.pk,
                        returncode=None,
                        reason=MinerRun.StopReason.START_FAILED,
                        error=error,
                    )
                    state.current_run = None
                    state.advisory_pid = None
                    state.worker_id = ""
                    state.stable_since = None
                    state.next_retry_at = None
                    state.observed_state = MinerInstanceState.ObservedState.STOPPED
                    state.save(
                        update_fields=(
                            "current_run",
                            "advisory_pid",
                            "worker_id",
                            "stable_since",
                            "next_retry_at",
                            "observed_state",
                            "updated_at",
                        )
                    )
                    raise RuntimeError(error)
                managed = self._spawn_snapshot(run, command_id=command_id)
        except Exception as exc:
            owned = self.processes.get(run.account_id)
            if owned is not None and owned.run_id == run.pk:
                if not owned.cleanup_required:
                    owned.cleanup_required = True
                    owned.cleanup_error = safe_error(
                        f"Launch ownership transaction did not commit: {exc}"
                    )
                self._reconcile_required_cleanup(owned)
            raise
        if managed is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Launch transaction completed without a managed process.")
        return managed

    def start_account(
        self,
        account: MinerAccount,
        *,
        reset_failures: bool = False,
        command_id: int | None = None,
        prepared_run: MinerRun | None = None,
    ) -> ManagedProcess:
        if self._flush_pending_run_finalizations(account_id=account.pk):
            raise RuntimeError(
                "A previous launch is still awaiting durable run finalization; "
                "a replacement will not start yet."
            )
        existing = self.processes.get(account.pk)
        if existing is not None and existing.cleanup_required:
            self._reconcile_required_cleanup(existing)
            existing = self.processes.get(account.pk)
        if existing is not None and existing.pending_stop_reason:
            self.stop_account(
                account,
                reason=existing.pending_stop_reason,
                preserve_desired=True,
                final_observed=MinerInstanceState.ObservedState.UNKNOWN,
            )
            existing = self.processes.get(account.pk)
        if existing is not None and (
            existing.cleanup_required or existing.pending_stop_reason
        ):
            raise RuntimeError(
                "A previous miner run is still awaiting durable finalization; "
                "a replacement will not start yet."
            )
        if existing is not None and existing.process.poll() is None:
            if command_id is not None and not existing.confirmed:
                if existing.command_id and existing.command_id != command_id:
                    self._finish_command(
                        existing.command_id,
                        MinerCommand.Status.CANCELLED,
                        "Superseded by another start command.",
                    )
                existing.command_id = command_id
            return existing
        if existing is not None:
            returncode = existing.process.poll()
            finalized = self._handle_unexpected_exit(
                existing,
                returncode,
                schedule_recovery=command_id is None,
            )
            if not finalized:
                raise RuntimeError(
                    "An existing miner exit is still awaiting durable bookkeeping; "
                    "a replacement will not start yet."
                )
            if command_id is None:
                raise RuntimeError(
                    "An existing miner exit was detected and scheduled for supervised recovery."
                )

        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        if not account.is_active or not account.has_credentials:
            error = (
                "Account is archived."
                if not account.is_active
                else "Account has no stored Twitch password."
            )
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = error
            state.next_retry_at = self.now() + timedelta(
                seconds=self.options.degraded_retry_seconds
            )
            state.save(
                update_fields=("observed_state", "last_error", "next_retry_at", "updated_at")
            )
            raise ValueError(error)

        run: MinerRun | None = None
        recovery_taken_over = False
        try:
            run = prepared_run or services.create_launch_snapshot(
                account,
                worker_id=self.worker_id,
            )
            state = self._take_over_recovery(
                account,
                operation="start",
                reset_failures=reset_failures,
            )
            recovery_taken_over = True
            return self._spawn_snapshot_if_desired_running(run, command_id=command_id)
        except Exception as exc:
            if run is not None:
                owned = self.processes.get(account.pk)
                if owned is None or owned.run_id != run.pk:
                    self._finalize_or_remember(
                        run_id=run.pk,
                        account_id=run.account_id,
                        returncode=None,
                        reason=MinerRun.StopReason.START_FAILED,
                        error=exc,
                    )
            state.refresh_from_db()
            state.last_error = safe_error(exc)
            if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                state.observed_state = MinerInstanceState.ObservedState.STOPPED
                state.next_retry_at = None
                state.save(
                    update_fields=(
                        "observed_state",
                        "next_retry_at",
                        "last_error",
                        "updated_at",
                    )
                )
            elif not recovery_taken_over and self._scheduled_recovery_attempt(account):
                # Validation failed before command takeover. Preserve the exact
                # rapid attempt/deadline that was already durable.
                state.save(update_fields=("last_error", "updated_at"))
            else:
                state.observed_state = MinerInstanceState.ObservedState.DEGRADED
                state.next_retry_at = self.now() + timedelta(
                    seconds=self.options.degraded_retry_seconds
                )
                state.save(
                    update_fields=(
                        "observed_state",
                        "last_error",
                        "next_retry_at",
                        "updated_at",
                    )
                )
                self._schedule_open_recovery_if_unowned(account, state)
            raise

    def _close_run(
        self,
        run_id: int,
        *,
        returncode: int | None,
        reason: str,
        error: object = "",
    ) -> None:
        exit_code = returncode if returncode is not None and returncode >= 0 else None
        exit_signal = -returncode if returncode is not None and returncode < 0 else None
        MinerRun.objects.filter(pk=run_id, ended_at__isnull=True).update(
            ended_at=self.now(),
            exit_code=exit_code,
            exit_signal=exit_signal,
            stop_reason=reason,
            error=safe_error(error) if error else "",
        )

    def _cancel_attached_start(self, managed: ManagedProcess, except_command_id: int | None) -> None:
        if managed.command_id and managed.command_id != except_command_id:
            self._finish_command(
                managed.command_id,
                MinerCommand.Status.CANCELLED,
                "A newer lifecycle command stopped this startup.",
            )

    def _take_over_recovery(
        self,
        account: MinerAccount,
        *,
        operation: str,
        reset_failures: bool,
        preserve_attempt_id: int | None = None,
    ) -> MinerInstanceState:
        """Atomically transfer an open incident from auto-recovery to a validated launch."""

        now = self.now()
        with transaction.atomic():
            state, _ = MinerInstanceState.objects.select_for_update().get_or_create(
                account=account
            )
            active_attempts = RestartAttempt.objects.filter(
                incident__account=account,
                incident__status=MinerIncident.Status.OPEN,
                outcome__in=(
                    RestartAttempt.Outcome.SCHEDULED,
                    RestartAttempt.Outcome.STARTED,
                ),
            )
            if preserve_attempt_id is not None:
                # A live recovery child keeps its STARTED attempt until its
                # termination actually succeeds. If signaling fails, that same
                # child can still confirm and record SUCCEEDED truthfully.
                active_attempts = active_attempts.exclude(pk=preserve_attempt_id)
            active_attempts.update(
                outcome=RestartAttempt.Outcome.FAILED,
                finished_at=now,
                error=f"Recovery attempt was superseded by an explicit {operation} command.",
            )
            if reset_failures:
                state.retry_count = 0
            state.next_retry_at = None
            state.last_error = ""
            state.save(
                update_fields=(
                    "retry_count",
                    "next_retry_at",
                    "last_error",
                    "updated_at",
                )
            )
        return state

    def _scheduled_recovery_attempt(self, account: MinerAccount) -> RestartAttempt | None:
        return (
            RestartAttempt.objects.filter(
                incident__account=account,
                incident__status=MinerIncident.Status.OPEN,
                outcome=RestartAttempt.Outcome.SCHEDULED,
            )
            .order_by("scheduled_at", "attempt_number", "id")
            .first()
        )

    def _schedule_open_recovery_if_unowned(
        self,
        account: MinerAccount,
        state: MinerInstanceState,
    ) -> bool:
        if (
            self.processes.get(account.pk) is not None
            or state.desired_state != MinerInstanceState.DesiredState.RUNNING
        ):
            return False
        incident = MinerIncident.objects.filter(
            account=account,
            status=MinerIncident.Status.OPEN,
        ).first()
        if incident is None:
            return False
        self._schedule_recovery(state, incident)
        return True

    def stop_account(
        self,
        account: MinerAccount,
        *,
        reason: str = MinerRun.StopReason.ADMIN_STOP,
        preserve_desired: bool = False,
        final_observed: str | None = None,
        except_command_id: int | None = None,
    ) -> bool:
        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        managed = self.processes.get(account.pk)
        if managed is not None:
            existing_returncode = managed.process.poll()
            if existing_returncode is not None and not managed.pending_stop_reason:
                # The child died before this planned operation reached it. The
                # exit is still an accident and must not be rewritten as an
                # admin/config stop merely because detection raced a command.
                finalized = self._handle_unexpected_exit(
                    managed,
                    existing_returncode,
                    schedule_recovery=False,
                    record_when_stopped=True,
                )
                if not finalized:
                    raise RuntimeError(
                        "The exited miner is still awaiting durable bookkeeping."
                    )
                state.refresh_from_db()
                managed = None

        state.observed_state = MinerInstanceState.ObservedState.STOPPING
        state.next_retry_at = None
        state.save(update_fields=("observed_state", "next_retry_at", "updated_at"))

        forced = False
        returncode: int | None = None
        if managed is not None:
            self._cancel_attached_start(managed, except_command_id)
            self._write_managed_lifecycle(managed, "stop_requested", reason=reason)
            try:
                returncode, forced_now = self._terminate_owned_process(managed.process)
                forced = managed.pending_stop_forced or forced_now
            except Exception as exc:
                state.observed_state = MinerInstanceState.ObservedState.DEGRADED
                state.last_error = safe_error(f"Could not stop owned miner process: {exc}")
                state.last_heartbeat = self.now()
                state.save(
                    update_fields=(
                        "observed_state",
                        "last_error",
                        "last_heartbeat",
                        "updated_at",
                    )
                )
                raise
            managed.pending_stop_reason = managed.pending_stop_reason or reason
            managed.pending_stop_forced = forced
            self._fail_restart_attempt(
                managed.restart_attempt_id,
                f"Recovery attempt was superseded by planned stop reason {reason}.",
            )
            try:
                self._close_run(
                    managed.run_id,
                    returncode=returncode,
                    reason=managed.pending_stop_reason,
                    error="Forced kill after graceful-stop timeout." if forced else "",
                )
            except Exception as exc:
                # The child is dead, but its handle is intentionally retained
                # until the run-ending transaction succeeds. A later health
                # pass can retry this exact finalization without misclassifying
                # the planned stop as an accident.
                state.observed_state = MinerInstanceState.ObservedState.DEGRADED
                state.last_error = safe_error(f"Could not finalize stopped miner run: {exc}")
                state.last_heartbeat = self.now()
                state.save(
                    update_fields=(
                        "observed_state",
                        "last_error",
                        "last_heartbeat",
                        "updated_at",
                    )
                )
                raise
            if self.processes.get(account.pk) is managed:
                self._finalize_managed_log(
                    managed,
                    "run_finished",
                    reason=managed.pending_stop_reason,
                    returncode=returncode,
                    forced=forced,
                )
                self.processes.pop(account.pk, None)
        elif state.current_run_id:
            # PIDs are advisory.  Never signal an arbitrary persisted PID.
            self._close_run(
                state.current_run_id,
                returncode=None,
                reason=reason,
                error="No process owned by this supervisor.",
            )

        state.current_run = None
        state.advisory_pid = None
        state.worker_id = ""
        state.last_heartbeat = self.now()
        state.stable_since = None
        state.last_error = ""
        if state.authentication_status == MinerInstanceState.AuthenticationStatus.PENDING:
            state.authentication_status = MinerInstanceState.AuthenticationStatus.UNLINKED
            state.authentication_uri = ""
            state.authentication_code = ""
            state.authentication_expires_at = None
            state.authentication_error = ""
            state.authentication_updated_at = self.now()
        state.observed_state = final_observed or (
            MinerInstanceState.ObservedState.UNKNOWN
            if preserve_desired
            else MinerInstanceState.ObservedState.STOPPED
        )
        state.save(
            update_fields=(
                "current_run",
                "advisory_pid",
                "worker_id",
                "last_heartbeat",
                "stable_since",
                "last_error",
                "authentication_status",
                "authentication_uri",
                "authentication_code",
                "authentication_expires_at",
                "authentication_error",
                "authentication_updated_at",
                "observed_state",
                "updated_at",
            )
        )

        if not preserve_desired:
            self._close_incident_for_intentional_stop(account)
        return managed is not None

    def restart_account(
        self,
        account: MinerAccount,
        *,
        reason: str = MinerRun.StopReason.ADMIN_RESTART,
        reset_failures: bool = False,
        command_id: int | None = None,
        prepared_run: MinerRun | None = None,
    ) -> ManagedProcess:
        if self._flush_pending_run_finalizations(account_id=account.pk):
            raise RuntimeError(
                "A previous launch is still awaiting durable run finalization; "
                "a replacement will not restart yet."
        )
        run: MinerRun | None = None
        state: MinerInstanceState | None = None
        recovery_taken_over = False
        try:
            # Validation and the immutable snapshot happen before a healthy
            # child or active recovery attempt is touched. Bad YAML or an empty
            # preset therefore cannot cancel working recovery or cause an
            # avoidable outage.
            run = prepared_run or services.create_launch_snapshot(
                account,
                worker_id=self.worker_id,
            )
            existing = self.processes.get(account.pk)
            preserve_attempt_id = (
                existing.restart_attempt_id
                if existing is not None and existing.process.poll() is None
                else None
            )
            state = self._take_over_recovery(
                account,
                operation="restart",
                reset_failures=reset_failures,
                preserve_attempt_id=preserve_attempt_id,
            )
            recovery_taken_over = True
            state.observed_state = MinerInstanceState.ObservedState.RESTARTING
            state.save(update_fields=("observed_state", "updated_at"))
            if existing is not None:
                self._write_managed_lifecycle(
                    existing,
                    "restart_requested",
                    reason=reason,
                )
            self.stop_account(
                account,
                reason=reason,
                preserve_desired=True,
                final_observed=MinerInstanceState.ObservedState.RESTARTING,
                except_command_id=command_id,
            )
            return self._spawn_snapshot_if_desired_running(run, command_id=command_id)
        except Exception as exc:
            owned = self.processes.get(account.pk)
            if run is not None and (owned is None or owned.run_id != run.pk):
                self._finalize_or_remember(
                    run_id=run.pk,
                    account_id=run.account_id,
                    returncode=None,
                    reason=MinerRun.StopReason.START_FAILED,
                    error=exc,
                )
            try:
                state, _ = MinerInstanceState.objects.get_or_create(account=account)
                state.refresh_from_db()
                old_child_is_alive = (
                    owned is not None
                    and (run is None or owned.run_id != run.pk)
                    and owned.process.poll() is None
                )
                state.last_error = safe_error(exc)
                if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                    state.observed_state = MinerInstanceState.ObservedState.STOPPED
                    state.next_retry_at = None
                    state.save(
                        update_fields=(
                            "observed_state",
                            "last_error",
                            "next_retry_at",
                            "updated_at",
                        )
                    )
                elif not recovery_taken_over and self._scheduled_recovery_attempt(account):
                    # The new launch never took ownership, so its failure must
                    # not postpone the already-scheduled rapid recovery.
                    state.save(update_fields=("last_error", "updated_at"))
                else:
                    state.observed_state = (
                        MinerInstanceState.ObservedState.RUNNING
                        if old_child_is_alive and owned.confirmed
                        else MinerInstanceState.ObservedState.DEGRADED
                    )
                    state.next_retry_at = (
                        None
                        if old_child_is_alive
                        else self.now() + timedelta(seconds=self.options.degraded_retry_seconds)
                    )
                    state.save(
                        update_fields=(
                            "observed_state",
                            "last_error",
                            "next_retry_at",
                            "updated_at",
                        )
                    )
                    self._schedule_open_recovery_if_unowned(account, state)
            except Exception:
                # Preserve the original restart failure. The pending-run map or
                # startup reconciliation still guarantees the unused snapshot
                # will eventually close if bookkeeping remains unavailable.
                logger.exception(
                    "Could not persist restart failure telemetry for %s",
                    account.config_key,
                )
            raise

    # -- health, incidents, and recovery ------------------------------------

    def _open_exit_incident(
        self,
        account: MinerAccount,
        run_id: int,
        returncode: int | None,
    ) -> MinerIncident:
        incident = MinerIncident.objects.filter(
            account=account,
            status=MinerIncident.Status.OPEN,
        ).first()
        if incident is not None:
            return incident
        detail = f"Miner run {run_id} exited with return code {returncode}."
        try:
            # Keep an IntegrityError isolated to a savepoint so callers can use
            # this helper inside a larger atomic exit-bookkeeping transaction.
            with transaction.atomic():
                return MinerIncident.objects.create(
                    account=account,
                    run_id=run_id,
                    kind=MinerIncident.Kind.UNEXPECTED_EXIT,
                    status=MinerIncident.Status.OPEN,
                    summary="Miner stopped while its desired state was running.",
                    details=safe_error(detail),
                )
        except IntegrityError:
            return MinerIncident.objects.get(
                account=account,
                status=MinerIncident.Status.OPEN,
            )

    def _schedule_recovery(
        self,
        state: MinerInstanceState,
        incident: MinerIncident,
        *,
        immediate: bool = False,
    ) -> RestartAttempt:
        with transaction.atomic():
            locked_state = MinerInstanceState.objects.select_for_update().get(pk=state.pk)
            locked_incident = MinerIncident.objects.select_for_update().get(pk=incident.pk)

            sequence_number = locked_state.retry_count + 1
            rapid = sequence_number <= len(self.options.rapid_restart_backoff)
            attempt = (
                locked_incident.restart_attempts.select_for_update()
                .filter(outcome=RestartAttempt.Outcome.SCHEDULED)
                .order_by("scheduled_at", "attempt_number", "id")
                .first()
            )
            if attempt is None:
                # Attempt number is a durable incident audit ordinal.
                # retry_count is the resettable rapid/degraded backoff
                # position. Keeping them separate prevents a reset counter
                # from reusing a FAILED row and leaving the account forever
                # without a SCHEDULED attempt.
                last_ordinal = (
                    locked_incident.restart_attempts.aggregate(value=Max("attempt_number"))[
                        "value"
                    ]
                    or 0
                )
                delay = 0 if immediate else (
                    self.options.rapid_restart_backoff[sequence_number - 1]
                    if rapid
                    else self.options.degraded_retry_seconds
                )
                attempt = RestartAttempt.objects.create(
                    incident=locked_incident,
                    attempt_number=last_ordinal + 1,
                    scheduled_at=self.now() + timedelta(seconds=delay),
                    outcome=RestartAttempt.Outcome.SCHEDULED,
                )

            # Scheduling is idempotent. In particular, cleanup reconciliation
            # can arrive after a failed recovery path already scheduled its
            # successor; reusing that row preserves one deadline and one audit
            # attempt for the next actual launch.
            locked_state.next_retry_at = attempt.scheduled_at
            locked_state.observed_state = (
                MinerInstanceState.ObservedState.RESTARTING
                if rapid
                else MinerInstanceState.ObservedState.DEGRADED
            )
            locked_state.save(
                update_fields=("next_retry_at", "observed_state", "updated_at")
            )

        state.next_retry_at = locked_state.next_retry_at
        state.observed_state = locked_state.observed_state
        return attempt

    def _fail_restart_attempt(self, attempt_id: int | None, error: object) -> None:
        if attempt_id is None:
            return
        RestartAttempt.objects.filter(pk=attempt_id).exclude(
            outcome=RestartAttempt.Outcome.SUCCEEDED
        ).update(
            outcome=RestartAttempt.Outcome.FAILED,
            finished_at=self.now(),
            error=safe_error(error),
        )

    def _handle_unexpected_exit(
        self,
        managed: ManagedProcess,
        returncode: int | None,
        *,
        schedule_recovery: bool = True,
        record_when_stopped: bool = False,
    ) -> bool:
        if not managed.crash_logged:
            self._write_managed_lifecycle(
                managed,
                "crash_detected",
                returncode=returncode,
            )
            managed.crash_logged = True
        recovery_lifecycle_details: dict[str, object] | None = None
        try:
            with transaction.atomic():
                # Keep the exact dead process handle until all run/incident/state
                # bookkeeping commits. A transient SQLite error rolls back the
                # whole bundle and is retried by the next health pass.
                self._close_run(
                    managed.run_id,
                    returncode=returncode,
                    reason=MinerRun.StopReason.UNEXPECTED_EXIT,
                    error=f"Process exited unexpectedly with return code {returncode}.",
                )
                if managed.command_id:
                    self._finish_command(
                        managed.command_id,
                        MinerCommand.Status.FAILED,
                        f"Miner exited during startup with return code {returncode}.",
                    )
                self._fail_restart_attempt(
                    managed.restart_attempt_id,
                    f"Miner exited with return code {returncode}.",
                )

                account = MinerAccount.objects.get(pk=managed.account_id)
                state, _ = MinerInstanceState.objects.select_for_update().get_or_create(
                    account=account
                )
                state.current_run = None
                state.advisory_pid = None
                state.worker_id = ""
                state.stable_since = None
                state.last_error = safe_error(f"Miner exited with return code {returncode}.")
                # Persist ownership removal before scheduling. The scheduler
                # reloads and locks a fresh state row, so keeping these changes
                # only on this Python object would leave an ended run and PID
                # displayed as current until a later launch.
                state.save(
                    update_fields=(
                        "current_run",
                        "advisory_pid",
                        "worker_id",
                        "stable_since",
                        "last_error",
                        "updated_at",
                    )
                )
                desired_stopped = state.desired_state == MinerInstanceState.DesiredState.STOPPED
                if desired_stopped and not record_when_stopped:
                    state.observed_state = MinerInstanceState.ObservedState.STOPPED
                    state.next_retry_at = None
                    state.save(
                        update_fields=(
                            "current_run",
                            "advisory_pid",
                            "worker_id",
                            "stable_since",
                            "last_error",
                            "observed_state",
                            "next_retry_at",
                            "updated_at",
                        )
                    )
                else:
                    incident = self._open_exit_incident(account, managed.run_id, returncode)
                    if not schedule_recovery:
                        state.observed_state = (
                            MinerInstanceState.ObservedState.STOPPED
                            if desired_stopped
                            else MinerInstanceState.ObservedState.UNKNOWN
                        )
                        state.next_retry_at = None
                        state.save(
                            update_fields=(
                                "current_run",
                                "advisory_pid",
                                "worker_id",
                                "stable_since",
                                "last_error",
                                "observed_state",
                                "next_retry_at",
                                "updated_at",
                            )
                        )
                        if desired_stopped:
                            incident.status = MinerIncident.Status.RECOVERED
                            incident.recovered_at = self.now()
                            incident.details = safe_error(
                                f"{incident.details} Recovery was not started because an "
                                "intentional stop had already become the desired state."
                            )
                            incident.save(
                                update_fields=(
                                    "status",
                                    "recovered_at",
                                    "details",
                                    "updated_at",
                                )
                            )
                    else:
                        attempt = self._schedule_recovery(state, incident)
                        recovery_lifecycle_details = {
                            "attempt": attempt.attempt_number,
                            "scheduled_at": attempt.scheduled_at.isoformat(),
                        }
        except Exception as exc:
            logger.exception(
                "Unexpected-exit bookkeeping remains pending for run %s: %s",
                managed.run_id,
                safe_error(exc),
            )
            return False

        if recovery_lifecycle_details is not None:
            self._write_managed_lifecycle(
                managed,
                "recovery_scheduled",
                **recovery_lifecycle_details,
            )

        if self.processes.get(managed.account_id) is managed:
            self._finalize_managed_log(
                managed,
                "unexpected_exit",
                returncode=returncode,
                recovery_requested=schedule_recovery,
            )
            self.processes.pop(managed.account_id, None)
        return True

    def _confirm_startup(self, managed: ManagedProcess) -> None:
        now = self.now()
        command_id = managed.command_id
        restart_attempt_id = managed.restart_attempt_id
        with transaction.atomic():
            updated_run = MinerRun.objects.filter(
                pk=managed.run_id,
                ended_at__isnull=True,
            ).update(startup_confirmed_at=now)
            if updated_run != 1:
                raise RuntimeError(
                    f"Run {managed.run_id} could not be confirmed because it is no longer active."
                )

            state = MinerInstanceState.objects.select_for_update().get(
                account_id=managed.account_id
            )
            state.observed_state = MinerInstanceState.ObservedState.RUNNING
            state.stable_since = now
            state.next_retry_at = None
            state.last_error = ""
            state.last_heartbeat = now
            state.authentication_status = MinerInstanceState.AuthenticationStatus.AUTHENTICATED
            state.authentication_uri = ""
            state.authentication_code = ""
            state.authentication_expires_at = None
            state.authentication_error = ""
            state.authentication_updated_at = now
            state.save(
                update_fields=(
                    "observed_state",
                    "stable_since",
                    "next_retry_at",
                    "last_error",
                    "last_heartbeat",
                    "authentication_status",
                    "authentication_uri",
                    "authentication_code",
                    "authentication_expires_at",
                    "authentication_error",
                    "authentication_updated_at",
                    "updated_at",
                )
            )

            if command_id:
                self._finish_command(command_id, MinerCommand.Status.SUCCEEDED)

            may_close_incident = restart_attempt_id is None
            if restart_attempt_id is not None:
                # An explicit restart can supersede a live recovery child
                # between spawn and confirmation. Never rewrite that terminal
                # FAILED attempt back to SUCCEEDED or close its incident before
                # the explicit replacement is healthy.
                may_close_incident = (
                    RestartAttempt.objects.filter(
                        pk=restart_attempt_id,
                        outcome=RestartAttempt.Outcome.STARTED,
                    ).update(
                        outcome=RestartAttempt.Outcome.SUCCEEDED,
                        finished_at=now,
                        error="",
                    )
                    == 1
                )
                if (
                    not may_close_incident
                    and state.desired_state == MinerInstanceState.DesiredState.RUNNING
                ):
                    # A validated explicit restart can supersede this attempt
                    # and then fail to stop the still-live recovery child. If
                    # that original child nevertheless becomes healthy after
                    # the command is terminal, it has recovered the farm and
                    # must not leave the incident open forever. A newer restart
                    # keeps ownership of the incident until its own result.
                    may_close_incident = not MinerCommand.objects.filter(
                        account_id=managed.account_id,
                        action=MinerCommand.Action.RESTART,
                        status__in=(
                            MinerCommand.Status.QUEUED,
                            MinerCommand.Status.LEASED,
                        ),
                    ).exists()
            if may_close_incident:
                MinerIncident.objects.filter(
                    account_id=managed.account_id,
                    status=MinerIncident.Status.OPEN,
                ).update(status=MinerIncident.Status.RECOVERED, recovered_at=now)

        self._write_managed_lifecycle(managed, "startup_confirmed")

        # Only publish in-memory confirmation after every durable record
        # commits. A transient SQLite error leaves this child eligible for the
        # next health pass to retry confirmation.
        managed.confirmed = True
        managed.command_id = None
        managed.restart_attempt_id = None

    def _persist_watching_event(
        self,
        managed: ManagedProcess,
        event: dict[str, object],
    ) -> None:
        state = MinerInstanceState.objects.get(account_id=managed.account_id)
        if (
            self.processes.get(managed.account_id) is not managed
            or state.current_run_id != managed.run_id
            or state.worker_id != self.worker_id
        ):
            return
        run = state.current_run
        if run is None:
            return
        by_name = {
            channel.casefold(): channel
            for channel in run.channels
            if isinstance(channel, str)
        }
        requested = [str(channel).casefold() for channel in event["channels"]]
        if any(channel not in by_name for channel in requested):
            self._write_managed_lifecycle(
                managed,
                "watching_channels_rejected",
                error="Reported channel is not in the launch snapshot.",
            )
            return
        selected = [
            channel
            for channel in run.channels
            if isinstance(channel, str) and channel.casefold() in requested
        ]
        state.watching_channels = selected
        state.watching_updated_at = self.now()
        state.save(
            update_fields=(
                "watching_channels",
                "watching_updated_at",
                "updated_at",
            )
        )

    def _consume_control_events(self, managed: ManagedProcess) -> str | None:
        """Persist validated child events; return a terminal authentication error."""

        terminal_error = None
        while True:
            try:
                event = managed.control_events.get_nowait()
            except queue.Empty:
                break
            kind = event["event"]
            now = self.now()
            state = MinerInstanceState.objects.get(account_id=managed.account_id)
            if kind == "device_code":
                expires_in = int(event["expires_in"])
                state.authentication_status = MinerInstanceState.AuthenticationStatus.PENDING
                state.authentication_uri = str(event["verification_uri"])
                state.authentication_code = str(event["user_code"])
                state.authentication_expires_at = now + timedelta(seconds=expires_in)
                state.authentication_error = ""
                state.authentication_updated_at = now
                state.save(
                    update_fields=(
                        "authentication_status",
                        "authentication_uri",
                        "authentication_code",
                        "authentication_expires_at",
                        "authentication_error",
                        "authentication_updated_at",
                        "updated_at",
                    )
                )
                managed.authentication_deadline = min(
                    managed.authentication_deadline or float("inf"),
                    self.monotonic() + expires_in,
                )
                self._write_managed_lifecycle(
                    managed,
                    "device_code",
                    activation_url=state.authentication_uri,
                    user_code=state.authentication_code,
                    expires_at=state.authentication_expires_at.isoformat(),
                )
            elif kind == "authenticated":
                managed.authenticated = True
                state.authentication_status = MinerInstanceState.AuthenticationStatus.AUTHENTICATED
                state.authentication_uri = ""
                state.authentication_code = ""
                state.authentication_expires_at = None
                state.authentication_error = ""
                state.authentication_updated_at = now
                state.save(
                    update_fields=(
                        "authentication_status",
                        "authentication_uri",
                        "authentication_code",
                        "authentication_expires_at",
                        "authentication_error",
                        "authentication_updated_at",
                        "updated_at",
                    )
                )
                self._write_managed_lifecycle(managed, "authenticated")
            elif kind == "authentication_failed":
                terminal_error = safe_error(event.get("error") or "Twitch authentication failed.")
                break
        if terminal_error is None:
            watching_event = managed.pop_watching_event()
            if watching_event is not None:
                self._persist_watching_event(managed, watching_event)
        return terminal_error

    def _fail_authentication(self, managed: ManagedProcess, error: object) -> None:
        safe = safe_error(error)
        command_id = managed.command_id
        self._write_managed_lifecycle(managed, "authentication_failed", error=safe)
        state = MinerInstanceState.objects.get(account_id=managed.account_id)
        state.desired_state = MinerInstanceState.DesiredState.STOPPED
        state.authentication_status = MinerInstanceState.AuthenticationStatus.REAUTH_REQUIRED
        state.authentication_uri = ""
        state.authentication_code = ""
        state.authentication_expires_at = None
        state.authentication_error = safe
        state.authentication_updated_at = self.now()
        state.save(
            update_fields=(
                "desired_state",
                "authentication_status",
                "authentication_uri",
                "authentication_code",
                "authentication_expires_at",
                "authentication_error",
                "authentication_updated_at",
                "updated_at",
            )
        )
        managed.pending_stop_reason = MinerRun.StopReason.AUTHENTICATION_FAILED
        self.stop_account(
            state.account,
            reason=MinerRun.StopReason.AUTHENTICATION_FAILED,
            preserve_desired=False,
            except_command_id=command_id,
        )
        if command_id:
            self._finish_command(command_id, MinerCommand.Status.FAILED, safe)

    def _close_incident_for_intentional_stop(
        self,
        account: MinerAccount,
        *,
        attempt_error: str = "Recovery was cancelled by an intentional stop.",
        incident_details: str = "Recovery was cancelled by an intentional admin stop.",
    ) -> None:
        now = self.now()
        incidents = MinerIncident.objects.filter(
            account=account,
            status=MinerIncident.Status.OPEN,
        )
        attempt_ids = RestartAttempt.objects.filter(
            incident__in=incidents,
            outcome__in=(RestartAttempt.Outcome.SCHEDULED, RestartAttempt.Outcome.STARTED),
        ).values_list("pk", flat=True)
        RestartAttempt.objects.filter(pk__in=attempt_ids).update(
            outcome=RestartAttempt.Outcome.FAILED,
            finished_at=now,
            error=attempt_error,
        )
        incidents.update(
            status=MinerIncident.Status.RECOVERED,
            recovered_at=now,
            details=incident_details,
        )

    def _reconcile_required_cleanup(self, managed: ManagedProcess) -> None:
        """Retry cleanup for a child whose post-spawn persistence failed."""

        account = MinerAccount.objects.get(pk=managed.account_id)
        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        try:
            returncode, forced = self._terminate_owned_process(managed.process)
        except Exception as exc:
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = safe_error(
                f"Spawn cleanup is still pending: {managed.cleanup_error}; {exc}"
            )
            state.last_heartbeat = self.now()
            state.save(
                update_fields=(
                    "observed_state",
                    "last_error",
                    "last_heartbeat",
                    "updated_at",
                )
            )
            return

        try:
            self._close_run(
                managed.run_id,
                returncode=returncode,
                reason=MinerRun.StopReason.START_FAILED,
                error=(
                    f"{managed.cleanup_error} Forced kill was required."
                    if forced
                    else managed.cleanup_error
                ),
            )
        except Exception as exc:
            # Retain the now-dead handle as a finalization tombstone. This keeps
            # the run from being silently cleared as active on the next pass.
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = safe_error(f"Spawn cleanup could not finalize its run: {exc}")
            state.last_heartbeat = self.now()
            state.save(
                update_fields=(
                    "observed_state",
                    "last_error",
                    "last_heartbeat",
                    "updated_at",
                )
            )
            return

        if self.processes.get(managed.account_id) is managed:
            self._finalize_managed_log(
                managed,
                "start_failed",
                returncode=returncode,
                forced=forced,
                error=managed.cleanup_error,
            )
            self.processes.pop(managed.account_id, None)
        if managed.command_id:
            self._finish_command(
                managed.command_id,
                MinerCommand.Status.FAILED,
                managed.cleanup_error,
            )
            managed.command_id = None
        self._fail_restart_attempt(managed.restart_attempt_id, managed.cleanup_error)

        state.refresh_from_db()
        state.current_run = None
        state.advisory_pid = None
        state.worker_id = ""
        state.stable_since = None
        state.last_error = managed.cleanup_error
        state.save(
            update_fields=(
                "current_run",
                "advisory_pid",
                "worker_id",
                "stable_since",
                "last_error",
                "updated_at",
            )
        )
        incident = MinerIncident.objects.filter(
            account=account,
            status=MinerIncident.Status.OPEN,
        ).first()
        if (
            state.desired_state == MinerInstanceState.DesiredState.RUNNING
            and incident is not None
        ):
            self._schedule_recovery(state, incident)
        else:
            state.observed_state = (
                MinerInstanceState.ObservedState.DEGRADED
                if state.desired_state == MinerInstanceState.DesiredState.RUNNING
                else MinerInstanceState.ObservedState.STOPPED
            )
            state.next_retry_at = (
                self.now() + timedelta(seconds=self.options.degraded_retry_seconds)
                if state.desired_state == MinerInstanceState.DesiredState.RUNNING
                else None
            )
            state.save(
                update_fields=(
                    "current_run",
                    "advisory_pid",
                    "worker_id",
                    "stable_since",
                    "last_error",
                    "observed_state",
                    "next_retry_at",
                    "updated_at",
                )
            )

    def _perform_due_recoveries(self) -> int:
        now = self.now()
        recovered = 0
        states = MinerInstanceState.objects.select_related("account", "account__credential").filter(
            desired_state=MinerInstanceState.DesiredState.RUNNING,
            next_retry_at__lte=now,
            account__is_active=True,
            account__credential__isnull=False,
        )
        for state in states:
            if state.account_id in self.processes:
                continue
            if self._flush_pending_run_finalizations(account_id=state.account_id):
                # Recovery must never create a replacement while an earlier
                # run is still falsely active due to a transient finalization
                # failure. The global loop retries this durable close first.
                continue
            incident = MinerIncident.objects.filter(
                account=state.account,
                status=MinerIncident.Status.OPEN,
            ).first()
            if incident is None:
                try:
                    self.start_account(state.account)
                    recovered += 1
                except Exception:
                    logger.exception("Periodic start retry failed for %s", state.account.config_key)
                continue

            attempt = RestartAttempt.objects.filter(
                incident=incident,
                outcome=RestartAttempt.Outcome.SCHEDULED,
                scheduled_at__lte=now,
            ).order_by("attempt_number").first()
            if attempt is None:
                self._schedule_recovery(state, incident)
                continue

            try:
                # Claim the due attempt and its account state together. A DB
                # failure leaves both rows scheduled/due for a clean retry.
                with transaction.atomic():
                    state = (
                        MinerInstanceState.objects.select_for_update()
                        .select_related("account")
                        .get(pk=state.pk)
                    )
                    attempt = RestartAttempt.objects.select_for_update().get(pk=attempt.pk)
                    if (
                        state.desired_state != MinerInstanceState.DesiredState.RUNNING
                        or state.next_retry_at is None
                        or state.next_retry_at > now
                        or attempt.outcome != RestartAttempt.Outcome.SCHEDULED
                        or attempt.scheduled_at > now
                    ):
                        continue
                    state.retry_count += 1
                    state.next_retry_at = None
                    state.observed_state = MinerInstanceState.ObservedState.RESTARTING
                    state.save(
                        update_fields=(
                            "retry_count",
                            "next_retry_at",
                            "observed_state",
                            "updated_at",
                        )
                    )
                    attempt.started_at = now
                    attempt.outcome = RestartAttempt.Outcome.STARTED
                    attempt.save(update_fields=("started_at", "outcome"))
            except Exception:
                logger.exception(
                    "Could not claim recovery attempt for %s",
                    state.account.config_key,
                )
                continue

            run: MinerRun | None = None
            try:
                run = services.create_launch_snapshot(state.account, worker_id=self.worker_id)
                attempt.run = run
                attempt.save(update_fields=("run",))
                with transaction.atomic():
                    # Linearize manual STOP against the actual Popen and its
                    # ownership write. If STOP committed first, cancel this
                    # unused snapshot. If this transaction owns SQLite's writer
                    # slot first, STOP is ordered after the launch and the
                    # queued command will stop that owned child next.
                    locked_state = MinerInstanceState.objects.select_for_update().get(
                        pk=state.pk
                    )
                    locked_attempt = RestartAttempt.objects.select_for_update().get(
                        pk=attempt.pk
                    )
                    if (
                        locked_state.desired_state
                        != MinerInstanceState.DesiredState.RUNNING
                        or locked_attempt.outcome != RestartAttempt.Outcome.STARTED
                    ):
                        cancellation_error = (
                            "Recovery launch was cancelled because desired state or "
                            "attempt ownership changed before spawn."
                        )
                        self._close_run(
                            run.pk,
                            returncode=None,
                            reason=MinerRun.StopReason.START_FAILED,
                            error=cancellation_error,
                        )
                        locked_attempt.outcome = RestartAttempt.Outcome.FAILED
                        locked_attempt.finished_at = self.now()
                        locked_attempt.error = cancellation_error
                        locked_attempt.save(
                            update_fields=("outcome", "finished_at", "error")
                        )
                        locked_state.current_run = None
                        locked_state.advisory_pid = None
                        locked_state.worker_id = ""
                        locked_state.stable_since = None
                        locked_state.next_retry_at = None
                        locked_state.observed_state = (
                            MinerInstanceState.ObservedState.STOPPED
                            if locked_state.desired_state
                            == MinerInstanceState.DesiredState.STOPPED
                            else MinerInstanceState.ObservedState.UNKNOWN
                        )
                        locked_state.save(
                            update_fields=(
                                "current_run",
                                "advisory_pid",
                                "worker_id",
                                "stable_since",
                                "next_retry_at",
                                "observed_state",
                                "updated_at",
                            )
                        )
                        continue
                    self._spawn_snapshot(run, restart_attempt_id=attempt.pk)
                recovered += 1
            except Exception as exc:
                provisional_cleanup: ManagedProcess | None = None
                if run is not None:
                    owned = self.processes.get(state.account_id)
                    if owned is None or owned.run_id != run.pk:
                        self._finalize_or_remember(
                            run_id=run.pk,
                            account_id=run.account_id,
                            returncode=None,
                            reason=MinerRun.StopReason.START_FAILED,
                            error=exc,
                        )
                    else:
                        # _spawn_snapshot can return before the enclosing
                        # state-lock transaction commits. If that commit (or
                        # any later statement in the block) fails, its child is
                        # provisional: never let health confirm ownership that
                        # SQLite rolled back.
                        if not owned.cleanup_required:
                            owned.cleanup_required = True
                            owned.cleanup_error = safe_error(
                                f"Recovery spawn transaction did not commit: {exc}"
                            )
                        provisional_cleanup = owned
                self._fail_restart_attempt(attempt.pk, exc)
                state.refresh_from_db()
                state.current_run = None
                state.advisory_pid = None
                state.worker_id = ""
                state.last_error = safe_error(exc)
                if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                    state.observed_state = MinerInstanceState.ObservedState.STOPPED
                    state.next_retry_at = None
                    state.save(
                        update_fields=(
                            "current_run",
                            "advisory_pid",
                            "worker_id",
                            "last_error",
                            "observed_state",
                            "next_retry_at",
                            "updated_at",
                        )
                    )
                else:
                    state.save(
                        update_fields=(
                            "current_run",
                            "advisory_pid",
                            "worker_id",
                            "last_error",
                            "updated_at",
                        )
                    )
                    self._schedule_recovery(state, incident)
                if (
                    provisional_cleanup is not None
                    and self.processes.get(state.account_id) is provisional_cleanup
                ):
                    self._reconcile_required_cleanup(provisional_cleanup)
                logger.exception("Recovery attempt failed for %s", state.account.config_key)
        return recovered

    def check_health(self) -> None:
        now = self.now()
        for account_id, managed in list(self.processes.items()):
            if managed.cleanup_required:
                self._reconcile_required_cleanup(managed)
                continue
            state = MinerInstanceState.objects.get(account_id=account_id)
            authentication_error = self._consume_control_events(managed)
            if authentication_error:
                self._fail_authentication(managed, authentication_error)
                continue
            if managed.pending_stop_reason:
                # The child was already stopped intentionally, but the durable
                # run-ending write failed. Retry that exact planned reason;
                # never reinterpret the retained dead handle as a crash.
                preserve_desired = (
                    state.desired_state == MinerInstanceState.DesiredState.RUNNING
                )
                self.stop_account(
                    state.account,
                    reason=managed.pending_stop_reason,
                    preserve_desired=preserve_desired,
                    final_observed=(
                        MinerInstanceState.ObservedState.RESTARTING
                        if preserve_desired
                        else MinerInstanceState.ObservedState.STOPPED
                    ),
                )
                continue
            if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                self.stop_account(
                    state.account,
                    reason=MinerRun.StopReason.ADMIN_STOP,
                    preserve_desired=False,
                )
                continue
            returncode = managed.process.poll()
            if returncode is not None:
                if managed.auth_required and not managed.authenticated:
                    self._fail_authentication(
                        managed,
                        "The miner exited before Twitch authentication completed.",
                    )
                    continue
                self._handle_unexpected_exit(managed, returncode)
                continue
            if (
                managed.auth_required
                and not managed.authenticated
                and managed.authentication_deadline is not None
                and self.monotonic() >= managed.authentication_deadline
            ):
                self._fail_authentication(
                    managed,
                    "Twitch activation expired or the authentication handshake timed out.",
                )
                continue
            if (
                not managed.confirmed
                and (not managed.auth_required or managed.authenticated)
                and self.monotonic() - managed.spawned_monotonic
                >= self.options.startup_grace_seconds
            ):
                self._confirm_startup(managed)
                state.refresh_from_db()
            if (
                managed.confirmed
                and state.retry_count
                and state.stable_since
                and (now - state.stable_since).total_seconds()
                >= self.options.stable_reset_seconds
            ):
                state.retry_count = 0
                state.save(update_fields=("retry_count", "updated_at"))

        self.reconcile_desired_state()

    def reconcile_desired_state(self) -> None:
        """Converge missing processes without trusting persisted PID values."""

        states = MinerInstanceState.objects.select_related("account", "account__credential").all()
        for state in states:
            managed = self.processes.get(state.account_id)
            if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                if managed is not None:
                    self.stop_account(state.account, preserve_desired=False)
                else:
                    with transaction.atomic():
                        locked_state = (
                            MinerInstanceState.objects.select_for_update()
                            .select_related("account")
                            .get(pk=state.pk)
                        )
                        if (
                            locked_state.desired_state
                            != MinerInstanceState.DesiredState.STOPPED
                        ):
                            continue
                        locked_state.observed_state = (
                            MinerInstanceState.ObservedState.STOPPED
                        )
                        locked_state.advisory_pid = None
                        locked_state.current_run = None
                        locked_state.worker_id = ""
                        locked_state.next_retry_at = None
                        locked_state.save(
                            update_fields=(
                                "observed_state",
                                "advisory_pid",
                                "current_run",
                                "worker_id",
                                "next_retry_at",
                                "updated_at",
                            )
                        )
                        if locked_state.account.is_active:
                            self._close_incident_for_intentional_stop(
                                locked_state.account
                            )
                        else:
                            self._close_incident_for_intentional_stop(
                                locked_state.account,
                                attempt_error=(
                                    "Recovery was cancelled because the account is archived."
                                ),
                                incident_details=(
                                    "Recovery was cancelled because this account is archived."
                                ),
                            )
                continue
            if not state.account.is_active or not state.account.has_credentials:
                with transaction.atomic():
                    locked_state = MinerInstanceState.objects.select_for_update().get(pk=state.pk)
                    locked_state.desired_state = MinerInstanceState.DesiredState.STOPPED
                    locked_state.observed_state = MinerInstanceState.ObservedState.STOPPED
                    locked_state.next_retry_at = None
                    locked_state.last_error = (
                        "Account is archived."
                        if not state.account.is_active
                        else "Account has no stored Twitch password."
                    )
                    locked_state.save(
                        update_fields=(
                            "desired_state",
                            "observed_state",
                            "next_retry_at",
                            "last_error",
                            "updated_at",
                        )
                    )
                if managed is not None:
                    self.stop_account(state.account, preserve_desired=False)
                continue
            if managed is not None or state.next_retry_at is not None:
                continue
            try:
                self.start_account(state.account)
            except Exception:
                logger.exception("Desired-state start failed for %s", state.account.config_key)

    def reconcile_fingerprints(self) -> None:
        """Restart only when a newly validated launch specification differs."""

        self.reconcile_desired_state()
        for managed in list(self.processes.values()):
            if not managed.confirmed or managed.process.poll() is not None:
                continue
            run = MinerRun.objects.select_related("account").get(pk=managed.run_id)
            try:
                resolution = services.resolve_channels(run.account)
                fingerprint = getattr(
                    resolution,
                    "configuration_fingerprint",
                    getattr(resolution, "fingerprint", ""),
                )
                if fingerprint == run.configuration_fingerprint:
                    continue
                self.restart_account(
                    run.account,
                    reason=MinerRun.StopReason.CONFIG_RESTART,
                )
            except Exception as exc:
                # A bad new preset/default must never take down the healthy old run.
                MinerInstanceState.objects.filter(account_id=managed.account_id).update(
                    last_error=safe_error(f"Configuration check failed: {exc}")
                )
                logger.exception("Configuration reconciliation failed for %s", managed.account_key)

    # -- loop and shutdown ---------------------------------------------------

    def run_once(self, *, force_checks: bool = False) -> None:
        if not self._started:
            raise RuntimeError("MinerSupervisor.startup() must run before the loop.")
        self.heartbeat(force=force_checks)
        self._flush_pending_run_finalizations()
        self.process_pending_commands()
        current = self.monotonic()
        if force_checks or current - self._last_health >= self.options.health_poll_seconds:
            self.check_health()
            self._last_health = current
        self._perform_due_recoveries()
        if (
            force_checks
            or current - self._last_fingerprint >= self.options.fingerprint_poll_seconds
        ):
            services.purge_expired_legacy_import_drafts(at=self.now())
            self.reconcile_fingerprints()
            if getattr(settings, "TWITCH_FARM_LOG_WRITER", False):
                # Every registered handle may still own an open writer, including
                # dead-process tombstones awaiting durable database finalization.
                active_run_ids = {managed.run_id for managed in self.processes.values()}
                with self._pending_log_finalizations_lock:
                    active_run_ids.update(self._pending_log_finalizations)
                try:
                    recovery_errors = recover_account_log_archives(
                        active_run_ids=active_run_ids
                    )
                except Exception:
                    logger.exception("Account log archive recovery failed")
                else:
                    for recovery_error in recovery_errors:
                        logger.error("Account log recovery is pending: %s", recovery_error)
            self._last_fingerprint = current

    def run_forever(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        stop = should_stop or (lambda: False)
        while not stop():
            self.run_once()
            self.sleep(self.options.command_poll_seconds)

    def _cleanup_released_shutdown_process(self, managed: ManagedProcess) -> None:
        """Finish an old child without rewriting a newer supervisor's state."""

        returncode, forced_now = self._terminate_owned_process(managed.process)
        forced = managed.pending_stop_forced or forced_now
        reason = managed.pending_stop_reason or MinerRun.StopReason.SUPERVISOR_SHUTDOWN
        managed.pending_stop_reason = reason
        managed.pending_stop_forced = forced
        self._fail_restart_attempt(
            managed.restart_attempt_id,
            f"Recovery attempt was superseded by planned stop reason {reason}.",
        )
        self._close_run(
            managed.run_id,
            returncode=returncode,
            reason=reason,
            error="Forced kill after graceful-stop timeout." if forced else "",
        )
        # A newer singleton may already have reconciled this old run and started
        # a replacement. Clear state only while it still names our exact worker
        # and run; the process handle and historical run row remain safe to
        # finalize independently after singleton ownership has been released.
        now = self.now()
        MinerInstanceState.objects.filter(
            account_id=managed.account_id,
            worker_id=self.worker_id,
            current_run_id=managed.run_id,
        ).update(
            current_run=None,
            advisory_pid=None,
            worker_id="",
            observed_state=MinerInstanceState.ObservedState.UNKNOWN,
            next_retry_at=None,
            stable_since=None,
            watching_channels=[],
            watching_updated_at=None,
            last_heartbeat=now,
            last_error="",
            updated_at=now,
        )
        if self.processes.get(managed.account_id) is managed:
            self._finalize_managed_log(
                managed,
                "run_finished",
                reason=reason,
                returncode=returncode,
                forced=forced,
            )
            self.processes.pop(managed.account_id, None)

    def shutdown(self) -> None:
        if (
            not self._started
            and self._lock_handle is None
            and not self.processes
            and not self._shutdown_incomplete
        ):
            return

        self._shutdown_incomplete = True
        logger.info("Miner supervisor shutdown requested: worker=%s", self.worker_id)
        owns_singleton = self._started
        errors: list[BaseException] = []
        try:
            try:
                account_ids = list(self.processes)
            except BaseException as exc:
                errors.append(exc)
                account_ids = []

            for account_id in account_ids:
                try:
                    if owns_singleton:
                        account = MinerAccount.objects.get(pk=account_id)
                        self.stop_account(
                            account,
                            reason=MinerRun.StopReason.SUPERVISOR_SHUTDOWN,
                            preserve_desired=True,
                            final_observed=MinerInstanceState.ObservedState.UNKNOWN,
                        )
                    else:
                        managed = self.processes.get(account_id)
                        if managed is not None:
                            self._cleanup_released_shutdown_process(managed)
                except BaseException as exc:
                    errors.append(exc)
            try:
                self._wait_for_pending_log_finalizations()
            except BaseException as exc:
                errors.append(exc)
        finally:
            try:
                try:
                    WorkerLease.objects.filter(
                        name=self.lease_name,
                        owner_id=self.worker_id,
                    ).delete()
                except BaseException as exc:
                    errors.append(exc)
            finally:
                try:
                    self._release_file_lock()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    self._started = False

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Miner supervisor shutdown failed.", errors)
        self._shutdown_incomplete = False

    cleanup = shutdown

    def __enter__(self) -> "MinerSupervisor":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.shutdown()
