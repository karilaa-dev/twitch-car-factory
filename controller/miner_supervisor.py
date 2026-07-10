"""Reliable single-owner supervisor for per-account Twitch miner processes.

Web requests only describe desired state and enqueue :class:`MinerCommand`
records.  This module is the sole process owner.  Its reconciliation loop makes
observed state converge on durable desired state, records every unexpected exit,
and keeps retrying without turning a broken miner into a tight restart loop.
"""

from __future__ import annotations

import fcntl
import ctypes
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, IO, Protocol

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from controller.models import (
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    RestartAttempt,
    WorkerLease,
)
from controller import services


logger = logging.getLogger(__name__)


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
    stop_timeout_seconds: float = 10.0
    rapid_restart_backoff: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0)
    degraded_retry_seconds: float = 300.0
    stable_reset_seconds: float = 600.0
    command_lease_seconds: float = 45.0
    worker_lease_seconds: float = 20.0
    worker_heartbeat_seconds: float = 5.0
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


_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization)\b\s*[:=]\s*([^\s,;]+)"
)


def safe_error(value: object, *, limit: int = 2000) -> str:
    """Bound and redact errors before persisting them or showing them in HTML."""

    text = str(value).replace("\x00", "")
    text = _SENSITIVE_VALUE.sub(r"\1=[redacted]", text)
    return text[:limit]


def linux_parent_death_hook(parent_pid: int) -> Callable[[], None]:
    """Return a child-side hook that prevents orphan miners on Linux.

    ``PR_SET_PDEATHSIG`` is inherited across ``exec``.  SIGKILL is intentional:
    this path is only used when the supervisor itself disappears without its
    normal ten-second graceful shutdown, and even the fake miner's
    ``ignore-term`` mode must not survive as an unowned process.  Comparing the
    actual parent PID closes the small race between ``fork`` and ``prctl`` and
    still works when the supervisor legitimately runs as container PID 1.
    """

    def configure_parent_death_signal() -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL) != 0:  # PR_SET_PDEATHSIG = 1
            os._exit(127)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)

    return configure_parent_death_signal


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
        self._lock_handle: IO[str] | None = None
        self._started = False
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

        run_ids = [managed.run_id for managed in self.processes.values()]
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
            services.sync_config_accounts()
            self.reconcile_startup()
            self.heartbeat(force=True)
            self._started = True
            self.reconcile_desired_state()
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
            else:
                raise ValueError(f"Unsupported miner command action: {command.action}")

            if managed.confirmed:
                self._finish_command(command.pk, MinerCommand.Status.SUCCEEDED)
                return False
            return True
        except Exception as exc:
            self._finish_command(command.pk, MinerCommand.Status.FAILED, exc)
            logger.exception("Miner command %s failed", command.pk)
            return False

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

    def build_process_command(self, run: MinerRun) -> list[str]:
        """Return argv containing identifiers only, never secrets or channels."""

        if self.options.fake_miner:
            return [
                sys.executable,
                str(Path(settings.BASE_DIR) / "manage.py"),
                "run_fake_miner",
                str(run.pk),
                run.account.config_key,
            ]
        return [
            sys.executable,
            "-m",
            "controller.miner_runner",
            str(run.pk),
            run.account.config_key,
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
        try:
            runtime_dir = Path(
                getattr(settings, "TWITCH_FARM_RUNTIME_DIR", settings.BASE_DIR / "runtime")
            )
            runtime_dir.mkdir(parents=True, exist_ok=True)
            child_environment = os.environ.copy()
            existing_pythonpath = child_environment.get("PYTHONPATH", "")
            child_environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(settings.BASE_DIR), existing_pythonpath) if part
            )
            popen_options = {
                # The upstream miner hardcodes ./cookies. Running children from
                # the worker-only runtime directory lets it refresh sessions
                # while the original cookie backup remains read-only.
                "cwd": runtime_dir,
                "env": child_environment,
                "close_fds": True,
                "start_new_session": True,
            }
            if sys.platform.startswith("linux"):
                popen_options["preexec_fn"] = linux_parent_death_hook(os.getpid())
            process = self.process_factory(self.build_process_command(run), **popen_options)
        except Exception as exc:
            error = safe_error(exc)
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
        )
        # Register ownership immediately after Popen. If any following database
        # write fails, cleanup can still signal this exact child and a failed
        # cleanup remains visible to later reconciliation.
        self.processes[run.account_id] = managed
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
                        self.processes.pop(run.account_id, None)
            raise
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
            self._handle_unexpected_exit(
                existing,
                returncode,
                schedule_recovery=command_id is None,
            )
            if command_id is None:
                raise RuntimeError(
                    "An existing miner exit was detected and scheduled for supervised recovery."
                )

        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        if reset_failures:
            state.retry_count = 0
            state.next_retry_at = None
            state.last_error = ""
            state.save(
                update_fields=("retry_count", "next_retry_at", "last_error", "updated_at")
            )

        if not account.is_configured:
            error = "Account is not present in the current config.yaml."
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = error
            state.next_retry_at = self.now() + timedelta(
                seconds=self.options.degraded_retry_seconds
            )
            state.save(
                update_fields=("observed_state", "last_error", "next_retry_at", "updated_at")
            )
            raise ValueError(error)

        try:
            run = prepared_run or services.create_launch_snapshot(
                account,
                worker_id=self.worker_id,
            )
            return self._spawn_snapshot(run, command_id=command_id)
        except Exception as exc:
            state.refresh_from_db()
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = safe_error(exc)
            state.next_retry_at = self.now() + timedelta(
                seconds=self.options.degraded_retry_seconds
            )
            state.save(
                update_fields=("observed_state", "last_error", "next_retry_at", "updated_at")
            )
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
                self._handle_unexpected_exit(
                    managed,
                    existing_returncode,
                    schedule_recovery=False,
                    record_when_stopped=True,
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
        # This validation and immutable snapshot happen before a healthy child
        # is touched.  Bad YAML or an empty preset therefore cannot cause an
        # avoidable outage.
        run = prepared_run or services.create_launch_snapshot(account, worker_id=self.worker_id)
        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        if reset_failures:
            state.retry_count = 0
            state.next_retry_at = None
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.save(
            update_fields=("retry_count", "next_retry_at", "observed_state", "updated_at")
        )
        try:
            self.stop_account(
                account,
                reason=reason,
                preserve_desired=True,
                final_observed=MinerInstanceState.ObservedState.RESTARTING,
                except_command_id=command_id,
            )
            return self._spawn_snapshot(run, command_id=command_id)
        except Exception as exc:
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
            state.observed_state = MinerInstanceState.ObservedState.DEGRADED
            state.last_error = safe_error(exc)
            state.next_retry_at = self.now() + timedelta(
                seconds=self.options.degraded_retry_seconds
            )
            state.save(
                update_fields=("observed_state", "last_error", "next_retry_at", "updated_at")
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
    ) -> RestartAttempt:
        attempt_number = state.retry_count + 1
        rapid = attempt_number <= len(self.options.rapid_restart_backoff)
        delay = (
            self.options.rapid_restart_backoff[attempt_number - 1]
            if rapid
            else self.options.degraded_retry_seconds
        )
        scheduled_at = self.now() + timedelta(seconds=delay)
        attempt, _ = RestartAttempt.objects.get_or_create(
            incident=incident,
            attempt_number=attempt_number,
            defaults={
                "scheduled_at": scheduled_at,
                "outcome": RestartAttempt.Outcome.SCHEDULED,
            },
        )
        state.next_retry_at = attempt.scheduled_at
        state.observed_state = (
            MinerInstanceState.ObservedState.RESTARTING
            if rapid
            else MinerInstanceState.ObservedState.DEGRADED
        )
        state.save(update_fields=("next_retry_at", "observed_state", "updated_at"))
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
    ) -> None:
        self.processes.pop(managed.account_id, None)
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
        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        state.current_run = None
        state.advisory_pid = None
        state.worker_id = ""
        state.stable_since = None
        state.last_error = safe_error(f"Miner exited with return code {returncode}.")
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
            return

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
                    f"{incident.details} Recovery was not started because an intentional "
                    "stop had already become the desired state."
                )
                incident.save(
                    update_fields=("status", "recovered_at", "details", "updated_at")
                )
            return
        self._schedule_recovery(state, incident)

    def _confirm_startup(self, managed: ManagedProcess) -> None:
        now = self.now()
        managed.confirmed = True
        MinerRun.objects.filter(pk=managed.run_id, ended_at__isnull=True).update(
            startup_confirmed_at=now
        )
        state = MinerInstanceState.objects.get(account_id=managed.account_id)
        state.observed_state = MinerInstanceState.ObservedState.RUNNING
        state.stable_since = now
        state.next_retry_at = None
        state.last_error = ""
        state.last_heartbeat = now
        state.save(
            update_fields=(
                "observed_state",
                "stable_since",
                "next_retry_at",
                "last_error",
                "last_heartbeat",
                "updated_at",
            )
        )

        if managed.command_id:
            self._finish_command(managed.command_id, MinerCommand.Status.SUCCEEDED)
            managed.command_id = None
        if managed.restart_attempt_id:
            RestartAttempt.objects.filter(pk=managed.restart_attempt_id).update(
                outcome=RestartAttempt.Outcome.SUCCEEDED,
                finished_at=now,
                error="",
            )
            managed.restart_attempt_id = None
        MinerIncident.objects.filter(
            account_id=managed.account_id,
            status=MinerIncident.Status.OPEN,
        ).update(status=MinerIncident.Status.RECOVERED, recovered_at=now)

    def _close_incident_for_intentional_stop(self, account: MinerAccount) -> None:
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
            error="Recovery was cancelled by an intentional stop.",
        )
        incidents.update(
            status=MinerIncident.Status.RECOVERED,
            recovered_at=now,
            details="Recovery was cancelled by an intentional admin stop.",
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
        states = MinerInstanceState.objects.select_related("account").filter(
            desired_state=MinerInstanceState.DesiredState.RUNNING,
            next_retry_at__lte=now,
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

            state.retry_count = max(state.retry_count, attempt.attempt_number)
            state.next_retry_at = None
            state.observed_state = MinerInstanceState.ObservedState.RESTARTING
            state.save(
                update_fields=("retry_count", "next_retry_at", "observed_state", "updated_at")
            )
            attempt.started_at = now
            attempt.outcome = RestartAttempt.Outcome.STARTED
            attempt.save(update_fields=("started_at", "outcome"))
            try:
                run = services.create_launch_snapshot(state.account, worker_id=self.worker_id)
                attempt.run = run
                attempt.save(update_fields=("run",))
                self._spawn_snapshot(run, restart_attempt_id=attempt.pk)
                recovered += 1
            except Exception as exc:
                self._fail_restart_attempt(attempt.pk, exc)
                state.refresh_from_db()
                state.current_run = None
                state.advisory_pid = None
                state.worker_id = ""
                state.last_error = safe_error(exc)
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
                logger.exception("Recovery attempt failed for %s", state.account.config_key)
        return recovered

    def check_health(self) -> None:
        now = self.now()
        for account_id, managed in list(self.processes.items()):
            if managed.cleanup_required:
                self._reconcile_required_cleanup(managed)
                continue
            state = MinerInstanceState.objects.get(account_id=account_id)
            if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                self.stop_account(
                    state.account,
                    reason=MinerRun.StopReason.ADMIN_STOP,
                    preserve_desired=False,
                )
                continue
            returncode = managed.process.poll()
            if returncode is not None:
                self._handle_unexpected_exit(managed, returncode)
                continue
            if (
                not managed.confirmed
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

        states = MinerInstanceState.objects.select_related("account").all()
        for state in states:
            managed = self.processes.get(state.account_id)
            if state.desired_state == MinerInstanceState.DesiredState.STOPPED:
                if managed is not None:
                    self.stop_account(state.account, preserve_desired=False)
                elif state.observed_state != MinerInstanceState.ObservedState.STOPPED:
                    state.observed_state = MinerInstanceState.ObservedState.STOPPED
                    state.advisory_pid = None
                    state.current_run = None
                    state.worker_id = ""
                    state.save(
                        update_fields=(
                            "observed_state",
                            "advisory_pid",
                            "current_run",
                            "worker_id",
                            "updated_at",
                        )
                    )
                continue
            if managed is not None or state.next_retry_at is not None:
                continue
            try:
                self.start_account(state.account)
            except Exception:
                logger.exception("Desired-state start failed for %s", state.account.config_key)

    def reconcile_fingerprints(self) -> None:
        """Restart only when a newly validated launch specification differs."""

        try:
            services.sync_config_accounts()
        except Exception as exc:
            error = safe_error(f"Configuration sync failed: {exc}")
            MinerInstanceState.objects.filter(
                desired_state=MinerInstanceState.DesiredState.RUNNING
            ).update(last_error=error)
            logger.exception("Configuration synchronization failed")
            return

        # Account additions and removals are part of configuration convergence,
        # not just channel fingerprint changes.  A newly autostarted account is
        # launched here; a removed account is intentionally stopped here.
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
                # A bad new preset/YAML must never take down the healthy old run.
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
            self.reconcile_fingerprints()
            self._last_fingerprint = current

    def run_forever(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        stop = should_stop or (lambda: False)
        while not stop():
            self.run_once()
            self.sleep(self.options.command_poll_seconds)

    def shutdown(self) -> None:
        if not self._started and self._lock_handle is None:
            return
        try:
            for account_id in list(self.processes):
                account = MinerAccount.objects.get(pk=account_id)
                self.stop_account(
                    account,
                    reason=MinerRun.StopReason.SUPERVISOR_SHUTDOWN,
                    preserve_desired=True,
                    final_observed=MinerInstanceState.ObservedState.UNKNOWN,
                )
            WorkerLease.objects.filter(
                name=self.lease_name,
                owner_id=self.worker_id,
            ).delete()
        finally:
            self._started = False
            self._release_file_lock()

    cleanup = shutdown

    def __enter__(self) -> "MinerSupervisor":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.shutdown()
