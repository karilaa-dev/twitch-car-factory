from __future__ import annotations

import json
import os
import pickle
import signal
import subprocess
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from controller.crypto import encrypt_json, encrypt_text
from controller import miner_runner
from controller.miner_runner import LaunchPayload, load_launch_payload, prepare_runtime_cookie
from controller.miner_supervisor import (
    ManagedProcess,
    MinerSupervisor,
    SupervisorAlreadyRunning,
    SupervisorOptions,
)
from controller.models import (
    AccountChannelSelection,
    AccountCredential,
    AccountSessionSeed,
    FarmConfiguration,
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    RestartAttempt,
    WorkerLease,
)
from controller.services import create_launch_snapshot, enqueue_command
from controller.runtime_logs import AccountRunLogWriter, read_run_log_page, summarize_run_log


def upsert_account(
    config_key: str,
    *,
    username: str,
    password: str,
    is_active: bool = True,
) -> MinerAccount:
    account, _ = MinerAccount.objects.update_or_create(
        config_key=config_key,
        defaults={
            "display_username": username,
            "is_active": is_active,
        },
    )
    AccountCredential.objects.update_or_create(
        account=account,
        defaults={"password_ciphertext": encrypt_text(password)},
    )
    AccountChannelSelection.objects.get_or_create(account=account)
    MinerInstanceState.objects.get_or_create(account=account)
    return account


def set_default_channels(channels: tuple[str, ...] = ("Alpha", "Beta")) -> None:
    configuration = FarmConfiguration.load()
    configuration.default_channels = list(channels)
    configuration.autostart_new_accounts = False
    configuration.save()


def configure_farm(
    *,
    channels: tuple[str, ...] = ("Alpha", "Beta"),
    include_second: bool = False,
) -> MinerAccount:
    set_default_channels(channels)
    primary = upsert_account(
        "primary",
        username="PrimaryUser",
        password="never-put-this-in-argv",
    )
    if include_second:
        upsert_account(
            "secondary",
            username="SecondaryUser",
            password="another-secret",
        )
    return primary


class FakeClock:
    def __init__(self) -> None:
        self.wall = timezone.now()
        self.ticks = 100.0

    def now(self):
        return self.wall

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.ticks += seconds


class FakeProcess:
    next_pid = 9000

    def __init__(
        self,
        *,
        ignore_terminate: bool = False,
        fail_terminate: bool = False,
    ) -> None:
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode: int | None = None
        self.ignore_terminate = ignore_terminate
        self.fail_terminate = fail_terminate
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if self.fail_terminate:
            raise OSError("simulated signal failure")
        self.terminated = True
        if not self.ignore_terminate:
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-miner", timeout)
        return self.returncode

    def crash(self, returncode: int = 17) -> None:
        self.returncode = returncode


class ProcessFactory:
    def __init__(
        self,
        *,
        ignore_terminate: bool = False,
        fail_terminate: bool = False,
    ) -> None:
        self.ignore_terminate = ignore_terminate
        self.fail_terminate = fail_terminate
        self.commands: list[list[str]] = []
        self.options: list[dict] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, command, **kwargs) -> FakeProcess:
        self.commands.append([str(value) for value in command])
        self.options.append(kwargs)
        process = FakeProcess(
            ignore_terminate=self.ignore_terminate,
            fail_terminate=self.fail_terminate,
        )
        self.processes.append(process)
        return process


def runtime_options(**overrides) -> SupervisorOptions:
    values = {
        "command_poll_seconds": 0,
        "health_poll_seconds": 0,
        "fingerprint_poll_seconds": 30,
        "startup_grace_seconds": 0,
        "stop_timeout_seconds": 0,
        "rapid_restart_backoff": (0, 0, 0, 0, 0),
        "degraded_retry_seconds": 100,
        "stable_reset_seconds": 600,
        "command_lease_seconds": 5,
        "worker_lease_seconds": 20,
        "worker_heartbeat_seconds": 0,
        "fake_miner": False,
        "lock_path": None,
    }
    values.update(overrides)
    return SupervisorOptions(**values)


def make_supervisor(clock: FakeClock, factory: ProcessFactory, **option_overrides):
    return MinerSupervisor(
        options=runtime_options(**option_overrides),
        process_factory=factory,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=lambda seconds: None,
        worker_id="test-worker",
        use_file_lock=False,
    )


@pytest.mark.django_db
def test_start_uses_snapshot_only_and_manual_stop_never_restarts(tmp_path, settings):
    settings.TWITCH_FARM_LOG_FILE = tmp_path / "logs" / "twitch-farm.log"
    settings.TWITCH_FARM_LOG_WRITER = True
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory(ignore_terminate=True)
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        start = enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)

        start.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        run = MinerRun.objects.get(pk=state.current_run_id)
        assert start.status == MinerCommand.Status.SUCCEEDED
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert run.channels == ["Alpha", "Beta"]
        assert run.startup_confirmed_at is not None

        argv = factory.commands[0]
        assert argv[-2:] == [str(run.pk), str(account.pk)]
        assert "never-put-this-in-argv" not in " ".join(argv)
        assert "Alpha" not in argv and "Beta" not in argv
        assert factory.options[0]["cwd"] == settings.TWITCH_FARM_RUNTIME_DIR
        assert str(settings.BASE_DIR) in factory.options[0]["env"]["PYTHONPATH"]
        assert factory.options[0]["env"]["TWITCH_FARM_LOG_WRITER"] == "0"
        assert factory.options[0]["env"]["PYTHONUNBUFFERED"] == "1"
        assert factory.options[0]["stdout"] == subprocess.PIPE
        assert factory.options[0]["stderr"] == subprocess.STDOUT

        stop = enqueue_command(account, MinerCommand.Action.STOP)
        supervisor.run_once(force_checks=True)
        stop.refresh_from_db()
        state.refresh_from_db()
        run.refresh_from_db()
        assert stop.status == MinerCommand.Status.SUCCEEDED
        assert state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert state.observed_state == MinerInstanceState.ObservedState.STOPPED
        assert state.advisory_pid is None
        assert run.stop_reason == MinerRun.StopReason.ADMIN_STOP
        assert factory.processes[0].killed is True
        assert MinerIncident.objects.filter(account=account).count() == 0
        forced_log = "\n".join(
            read_run_log_page(account_id=account.pk, run_id=run.pk)["lines"]
        )
        assert "forced_termination" in forced_log
        assert "final_exit" in forced_log

        clock.advance(1000)
        supervisor.run_once(force_checks=True)
        assert len(factory.processes) == 1
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_supervisor_finalizes_account_lifecycle_archive_on_normal_stop(tmp_path, settings):
    settings.TWITCH_FARM_LOG_FILE = tmp_path / "logs" / "twitch-farm.log"
    settings.TWITCH_FARM_LOG_WRITER = True
    settings.TWITCH_FARM_ACCOUNT_LOG_PART_BYTES = 1024 * 1024
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        run = MinerInstanceState.objects.get(account=account).current_run
        assert run is not None

        enqueue_command(account, MinerCommand.Action.STOP)
        supervisor.run_once(force_checks=True)

        summary = summarize_run_log(account.pk, run.pk)
        page = read_run_log_page(account_id=account.pk, run_id=run.pk)
        contents = "\n".join(page["lines"])
        assert summary.compressed_parts == 1
        assert not summary.compression_pending
        assert "launch_requested" in contents
        assert "process_started" in contents
        assert "startup_confirmed" in contents
        assert "stop_requested" in contents
        assert "run_finished" in contents
        assert "final_exit" in contents
        assert "reason=\"admin_stop\"" in contents
    finally:
        supervisor.shutdown()


def test_log_finalization_waits_for_late_output_after_the_bounded_join(tmp_path, settings):
    settings.TWITCH_FARM_LOG_FILE = tmp_path / "logs" / "twitch-farm.log"
    settings.TWITCH_FARM_LOG_WRITER = True
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    writer = AccountRunLogWriter(account_id=1, run_id=77, account_key="primary")

    class DeferredOutputThread:
        def __init__(self):
            self.alive = True
            self.join_timeouts: list[float | None] = []

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)
            if timeout is None:
                writer.write("late buffered miner output")
                self.alive = False

        def is_alive(self):
            return self.alive

    output_thread = DeferredOutputThread()
    managed = ManagedProcess(
        process=FakeProcess(),
        account_id=1,
        account_key="primary",
        run_id=77,
        spawned_monotonic=clock.monotonic(),
        output_thread=output_thread,  # type: ignore[arg-type]
        log_writer=writer,
    )

    supervisor._finalize_managed_log(managed, "run_finished", reason="admin_stop")
    supervisor._wait_for_pending_log_finalizations()

    contents = "\n".join(read_run_log_page(account_id=1, run_id=77)["lines"])
    assert output_thread.join_timeouts == [1.0, None]
    assert contents.index("late buffered miner output") < contents.index("final_exit")
    assert managed.log_writer is None


@pytest.mark.django_db
def test_supervisor_records_explicit_restart_in_old_and_new_run_logs(tmp_path, settings):
    settings.TWITCH_FARM_LOG_FILE = tmp_path / "logs" / "twitch-farm.log"
    settings.TWITCH_FARM_LOG_WRITER = True
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        old_run = MinerInstanceState.objects.get(account=account).current_run

        enqueue_command(account, MinerCommand.Action.RESTART)
        supervisor.run_once(force_checks=True)
        new_run = MinerInstanceState.objects.get(account=account).current_run

        assert old_run is not None and new_run is not None
        assert old_run.pk != new_run.pk
        old_log = "\n".join(
            read_run_log_page(account_id=account.pk, run_id=old_run.pk)["lines"]
        )
        new_log = "\n".join(
            read_run_log_page(account_id=account.pk, run_id=new_run.pk)["lines"]
        )
        assert "restart_requested" in old_log
        assert "stop_requested" in old_log
        assert "reason=\"admin_restart\"" in old_log
        assert "final_exit" in old_log
        assert "launch_requested" in new_log
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_unexpected_exit_records_incident_attempt_and_recovery(tmp_path, settings):
    settings.TWITCH_FARM_LOG_FILE = tmp_path / "logs" / "twitch-farm.log"
    settings.TWITCH_FARM_LOG_WRITER = True
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        failed_run = MinerInstanceState.objects.get(account=account).current_run

        factory.processes[0].crash(23)
        supervisor.run_once(force_checks=True)
        # The due zero-delay recovery was spawned after this cycle's health pass.
        supervisor.run_once(force_checks=True)

        failed_run.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        incident = MinerIncident.objects.get(account=account)
        attempt = RestartAttempt.objects.get(incident=incident, attempt_number=1)
        assert failed_run.stop_reason == MinerRun.StopReason.UNEXPECTED_EXIT
        assert failed_run.exit_code == 23
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
        assert attempt.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert attempt.run_id == state.current_run_id
        assert state.desired_state == MinerInstanceState.DesiredState.RUNNING
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert state.retry_count == 1
        assert len(factory.processes) == 2

        failed_log = "\n".join(
            read_run_log_page(account_id=account.pk, run_id=failed_run.pk)["lines"]
        )
        recovery_log = "\n".join(
            read_run_log_page(account_id=account.pk, run_id=state.current_run_id)["lines"]
        )
        assert "crash_detected" in failed_log
        assert "recovery_scheduled" in failed_log
        assert "unexpected_exit" in failed_log
        assert "final_exit" in failed_log
        assert "recovery_started" in recovery_log
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_manual_stop_committed_between_recovery_claim_and_spawn_prevents_launch(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery is due.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        original_snapshot = create_launch_snapshot
        stop_command = None

        def snapshot_then_stop(*args, **kwargs):
            nonlocal stop_command
            run = original_snapshot(*args, **kwargs)
            stop_command = enqueue_command(account, MinerCommand.Action.STOP)
            return run

        monkeypatch.setattr(
            "controller.miner_supervisor.services.create_launch_snapshot",
            snapshot_then_stop,
        )
        assert supervisor._perform_due_recoveries() == 0

        state.refresh_from_db()
        attempt.refresh_from_db()
        candidate = MinerRun.objects.get(account=account)
        assert stop_command is not None
        assert factory.processes == []
        assert supervisor.processes == {}
        assert state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert state.observed_state == MinerInstanceState.ObservedState.STOPPED
        assert state.current_run_id is None
        assert state.advisory_pid is None
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert "cancelled" in attempt.error.casefold()
        assert candidate.ended_at is not None
        assert candidate.stop_reason == MinerRun.StopReason.START_FAILED

        assert supervisor.process_pending_commands() == 1
        stop_command.refresh_from_db()
        incident.refresh_from_db()
        assert stop_command.status == MinerCommand.Status.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert len(factory.processes) == 0
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_recovery_spawn_transaction_failure_cleans_provisional_child(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery is due.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        original_spawn = supervisor._spawn_snapshot
        provisional = None

        def spawn_then_fail_transaction(*args, **kwargs):
            nonlocal provisional
            provisional = original_spawn(*args, **kwargs)
            raise OperationalError("simulated outer recovery transaction commit failure")

        monkeypatch.setattr(supervisor, "_spawn_snapshot", spawn_then_fail_transaction)
        assert supervisor._perform_due_recoveries() == 0

        state.refresh_from_db()
        attempt.refresh_from_db()
        run = MinerRun.objects.get(pk=attempt.run_id)
        successor = RestartAttempt.objects.get(incident=incident, attempt_number=2)
        assert provisional is not None
        assert provisional.confirmed is False
        assert provisional.process.poll() == -15
        assert account.pk not in supervisor.processes
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert successor.outcome == RestartAttempt.Outcome.SCHEDULED
        assert RestartAttempt.objects.filter(
            incident=incident,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        ).count() == 1
        assert run.ended_at is not None
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
        assert run.startup_confirmed_at is None
        assert state.current_run_id is None
        assert state.advisory_pid is None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_command_spawn_transaction_failure_cleans_provisional_child(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        original_spawn = supervisor._spawn_snapshot
        provisional = None

        def spawn_then_fail_transaction(*args, **kwargs):
            nonlocal provisional
            provisional = original_spawn(*args, **kwargs)
            raise OperationalError("simulated outer command transaction commit failure")

        monkeypatch.setattr(supervisor, "_spawn_snapshot", spawn_then_fail_transaction)
        command = enqueue_command(account, MinerCommand.Action.START)
        assert supervisor.process_pending_commands() == 1

        command.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        run = MinerRun.objects.get(account=account)
        assert provisional is not None
        assert provisional.confirmed is False
        assert provisional.process.poll() == -15
        assert command.status == MinerCommand.Status.FAILED
        assert account.pk not in supervisor.processes
        assert run.ended_at is not None
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
        assert run.startup_confirmed_at is None
        assert state.current_run_id is None
        assert state.advisory_pid is None
        assert state.observed_state == MinerInstanceState.ObservedState.DEGRADED

        supervisor.check_health()
        state.refresh_from_db()
        assert account.pk not in supervisor.processes
        assert state.observed_state != MinerInstanceState.ObservedState.RUNNING
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_manual_stop_committed_during_explicit_restart_prevents_replacement_spawn(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        old_run = MinerInstanceState.objects.get(account=account).current_run
        original_snapshot = create_launch_snapshot
        stop_command = None

        def snapshot_then_stop(*args, **kwargs):
            nonlocal stop_command
            run = original_snapshot(*args, **kwargs)
            stop_command = enqueue_command(account, MinerCommand.Action.STOP)
            return run

        monkeypatch.setattr(
            "controller.miner_supervisor.services.create_launch_snapshot",
            snapshot_then_stop,
        )
        restart = enqueue_command(account, MinerCommand.Action.RESTART)
        assert supervisor.process_pending_commands() == 2

        restart.refresh_from_db()
        stop_command.refresh_from_db()
        old_run.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        candidate = MinerRun.objects.exclude(pk=old_run.pk).get(account=account)
        assert restart.status == MinerCommand.Status.FAILED
        assert stop_command.status == MinerCommand.Status.SUCCEEDED
        assert state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert state.observed_state == MinerInstanceState.ObservedState.STOPPED
        assert state.current_run_id is None
        assert old_run.stop_reason == MinerRun.StopReason.ADMIN_RESTART
        assert candidate.stop_reason == MinerRun.StopReason.START_FAILED
        assert candidate.ended_at is not None
        assert len(factory.processes) == 1
        assert supervisor.processes == {}
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "active_outcome",
    (RestartAttempt.Outcome.SCHEDULED, RestartAttempt.Outcome.STARTED),
)
def test_explicit_restart_supersedes_active_recovery_attempts_until_confirmation(
    tmp_path,
    settings,
    active_outcome,
):
    configure_farm()
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.DEGRADED
        state.retry_count = 3
        state.next_retry_at = clock.now()
        state.last_error = "Previous recovery failed."
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Waiting for recovery.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=4,
            scheduled_at=clock.now(),
            started_at=(
                clock.now() if active_outcome == RestartAttempt.Outcome.STARTED else None
            ),
            outcome=active_outcome,
        )

        command = enqueue_command(
            account,
            MinerCommand.Action.RESTART,
            reason="Admin requested restart.",
        )

        state.refresh_from_db()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert state.retry_count == 3
        assert state.next_retry_at == clock.now()
        assert state.last_error == "Previous recovery failed."
        assert attempt.outcome == active_outcome
        assert attempt.finished_at is None
        assert incident.status == MinerIncident.Status.OPEN
        assert incident.recovered_at is None

        supervisor.run_once(force_checks=True)

        command.refresh_from_db()
        state.refresh_from_db()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        replacement = MinerRun.objects.get(account=account, ended_at__isnull=True)
        assert command.status == MinerCommand.Status.SUCCEEDED
        assert state.retry_count == 0
        assert state.next_retry_at is None
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert attempt.finished_at is not None
        assert "superseded by an explicit restart" in attempt.error.casefold()
        assert replacement.startup_confirmed_at is not None
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
        assert not RestartAttempt.objects.filter(
            incident=incident,
            outcome__in=(
                RestartAttempt.Outcome.SCHEDULED,
                RestartAttempt.Outcome.STARTED,
            ),
        ).exists()
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_live_recovery_may_confirm_before_explicit_restart_takes_over(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery child is starting.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )

        assert supervisor._perform_due_recoveries() == 1
        recovery_child = supervisor.processes[account.pk]
        attempt.refresh_from_db()
        assert recovery_child.confirmed is False
        assert attempt.outcome == RestartAttempt.Outcome.STARTED

        command = enqueue_command(account, MinerCommand.Action.RESTART)
        attempt.refresh_from_db()
        assert attempt.outcome == RestartAttempt.Outcome.STARTED

        # Reproduce a web request arriving after the loop's command pass but
        # before health confirmation of the already-spawned recovery child.
        supervisor.check_health()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert recovery_child.confirmed is True
        assert attempt.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None

        assert supervisor.process_pending_commands() == 1
        supervisor.check_health()

        command.refresh_from_db()
        incident.refresh_from_db()
        assert command.status == MinerCommand.Status.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
        assert supervisor.processes[account.pk].run_id != recovery_child.run_id
        assert len(factory.processes) == 2
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_invalid_explicit_restart_does_not_cancel_live_recovery(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery child is starting.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        assert supervisor._perform_due_recoveries() == 1
        recovery_child = supervisor.processes[account.pk]
        assert recovery_child.confirmed is False

        command = enqueue_command(account, MinerCommand.Action.RESTART)
        set_default_channels(())
        assert supervisor.process_pending_commands() == 1

        command.refresh_from_db()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert command.status == MinerCommand.Status.FAILED
        assert attempt.outcome == RestartAttempt.Outcome.STARTED
        assert attempt.finished_at is None
        assert incident.status == MinerIncident.Status.OPEN
        assert supervisor.processes[account.pk] is recovery_child
        assert recovery_child.process.poll() is None
        assert len(factory.processes) == 1
        assert MinerRun.objects.filter(account=account).count() == 1

        supervisor.check_health()

        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert recovery_child.confirmed is True
        assert attempt.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_invalid_restart_preserves_existing_rapid_recovery_deadline(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory, rapid_restart_backoff=(5, 15, 30, 60, 120))
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        rapid_deadline = clock.now() + timedelta(seconds=5)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = rapid_deadline
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Waiting for the first rapid retry.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=rapid_deadline,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )

        command = enqueue_command(account, MinerCommand.Action.RESTART)
        set_default_channels(())
        assert supervisor.process_pending_commands() == 1

        command.refresh_from_db()
        attempt.refresh_from_db()
        state.refresh_from_db()
        assert command.status == MinerCommand.Status.FAILED
        assert attempt.outcome == RestartAttempt.Outcome.SCHEDULED
        assert attempt.scheduled_at == rapid_deadline
        assert state.next_retry_at == rapid_deadline

        set_default_channels()
        clock.advance(5)
        assert supervisor._perform_due_recoveries() == 1
        attempt.refresh_from_db()
        assert attempt.outcome == RestartAttempt.Outcome.STARTED
        assert len(factory.processes) == 1
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_failed_validated_restart_takeover_schedules_new_rapid_attempt(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.retry_count = 4
        state.next_retry_at = clock.now() + timedelta(minutes=5)
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Explicit restart will take over recovery.",
        )
        previous = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=4,
            scheduled_at=state.next_retry_at,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )

        def fail_spawn(*args, **kwargs):
            raise OSError("simulated spawn handoff failure")

        monkeypatch.setattr(supervisor, "_spawn_snapshot", fail_spawn)
        command = enqueue_command(account, MinerCommand.Action.RESTART)
        assert supervisor.process_pending_commands() == 1

        command.refresh_from_db()
        previous.refresh_from_db()
        state.refresh_from_db()
        successor = RestartAttempt.objects.get(incident=incident, attempt_number=5)
        candidate = MinerRun.objects.get(account=account)
        assert command.status == MinerCommand.Status.FAILED
        assert previous.outcome == RestartAttempt.Outcome.FAILED
        assert successor.outcome == RestartAttempt.Outcome.SCHEDULED
        assert successor.scheduled_at == clock.now()
        assert state.retry_count == 0
        assert state.next_retry_at == successor.scheduled_at
        assert candidate.stop_reason == MinerRun.StopReason.START_FAILED
        assert candidate.ended_at is not None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_repeated_restart_coalesces_while_replacement_is_starting(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        assert len(factory.processes) == 1

        first_restart = enqueue_command(account, MinerCommand.Action.RESTART)
        assert supervisor.process_pending_commands() == 1
        first_restart.refresh_from_db()
        assert first_restart.status == MinerCommand.Status.LEASED
        assert supervisor.processes[account.pk].confirmed is False

        repeated_restart = enqueue_command(account, MinerCommand.Action.RESTART)
        assert repeated_restart.pk == first_restart.pk
        assert MinerCommand.objects.filter(
            account=account,
            action=MinerCommand.Action.RESTART,
        ).count() == 1
        assert supervisor.process_pending_commands() == 0

        supervisor.check_health()
        first_restart.refresh_from_db()
        assert first_restart.status == MinerCommand.Status.SUCCEEDED
        assert supervisor.processes[account.pk].confirmed is True
        assert len(factory.processes) == 2
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_failed_restart_signal_allows_surviving_recovery_attempt_to_succeed(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory(fail_terminate=True)
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    recovery_child = None
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery child is starting.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        assert supervisor._perform_due_recoveries() == 1
        recovery_child = supervisor.processes[account.pk]

        command = enqueue_command(account, MinerCommand.Action.RESTART)
        assert supervisor.process_pending_commands() == 1

        command.refresh_from_db()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert command.status == MinerCommand.Status.FAILED
        assert attempt.outcome == RestartAttempt.Outcome.STARTED
        assert attempt.error == ""
        assert incident.status == MinerIncident.Status.OPEN
        assert supervisor.processes[account.pk] is recovery_child
        assert recovery_child.process.poll() is None
        assert recovery_child.confirmed is False
        assert len(factory.processes) == 1

        supervisor.check_health()

        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert recovery_child.confirmed is True
        assert attempt.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
    finally:
        if recovery_child is not None:
            recovery_child.process.fail_terminate = False
        supervisor.shutdown()


@pytest.mark.django_db
def test_terminal_newer_restart_recovers_incident_for_confirmed_fallback_child(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory(fail_terminate=True)
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    recovery_child = None
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery child is starting.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        assert supervisor._perform_due_recoveries() == 1
        recovery_child = supervisor.processes[account.pk]

        first_restart = enqueue_command(account, MinerCommand.Action.RESTART)
        assert supervisor.process_pending_commands() == 1
        first_restart.refresh_from_db()
        assert first_restart.status == MinerCommand.Status.FAILED

        second_restart = enqueue_command(account, MinerCommand.Action.RESTART)
        supervisor.check_health()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert recovery_child.confirmed is True
        assert attempt.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED

        set_default_channels(())
        assert supervisor.process_pending_commands() == 1

        second_restart.refresh_from_db()
        incident.refresh_from_db()
        assert second_restart.status == MinerCommand.Status.FAILED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
        assert supervisor.processes[account.pk] is recovery_child
        assert recovery_child.process.poll() is None
        assert len(factory.processes) == 1
    finally:
        if recovery_child is not None:
            recovery_child.process.fail_terminate = False
        supervisor.shutdown()


@pytest.mark.django_db
def test_restart_bookkeeping_failure_finalizes_unused_snapshot_and_keeps_old_child(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        old_managed = supervisor.processes[account.pk]
        old_run = MinerRun.objects.get(pk=old_managed.run_id)
        original_save = MinerInstanceState.save

        def fail_restarting_save(instance, *args, **kwargs):
            if instance.observed_state == MinerInstanceState.ObservedState.RESTARTING:
                raise OperationalError("simulated restart bookkeeping failure")
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(MinerInstanceState, "save", fail_restarting_save)
        with pytest.raises(OperationalError):
            supervisor.restart_account(account, reset_failures=True)

        runs = list(MinerRun.objects.filter(account=account).order_by("started_at", "id"))
        assert len(runs) == 2
        unused_run = runs[1]
        old_run.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        assert supervisor.processes[account.pk] is old_managed
        assert old_managed.process.poll() is None
        assert old_run.ended_at is None
        assert unused_run.stop_reason == MinerRun.StopReason.START_FAILED
        assert unused_run.ended_at is not None
        assert state.current_run_id == old_run.pk
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert state.next_retry_at is None

        monkeypatch.setattr(MinerInstanceState, "save", original_save)
        replacement = supervisor.restart_account(account, reset_failures=True)
        supervisor.check_health()
        old_run.refresh_from_db()
        replacement_run = MinerRun.objects.get(pk=replacement.run_id)
        assert replacement is supervisor.processes[account.pk]
        assert replacement.run_id != old_run.pk
        assert old_run.stop_reason == MinerRun.StopReason.ADMIN_RESTART
        assert replacement_run.startup_confirmed_at is not None
        assert MinerRun.objects.filter(account=account, ended_at__isnull=True).count() == 1
        assert len(factory.processes) == 2
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_unexpected_exit_keeps_handle_until_run_and_incident_bookkeeping_commits(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        managed = supervisor.processes[account.pk]
        failed_run = MinerRun.objects.get(pk=managed.run_id)
        managed.process.crash(29)
        original_close_run = supervisor._close_run

        def fail_close_run(*args, **kwargs):
            raise OperationalError("simulated unexpected-exit finalization failure")

        monkeypatch.setattr(supervisor, "_close_run", fail_close_run)
        supervisor.check_health()

        failed_run.refresh_from_db()
        assert supervisor.processes[account.pk] is managed
        assert failed_run.ended_at is None
        assert MinerRun.objects.filter(account=account).count() == 1
        assert not MinerIncident.objects.filter(account=account).exists()
        state = MinerInstanceState.objects.get(account=account)
        heartbeat_before = state.last_heartbeat
        clock.advance(1)
        supervisor.heartbeat(force=True)
        state.refresh_from_db()
        assert state.last_heartbeat == heartbeat_before

        monkeypatch.setattr(supervisor, "_close_run", original_close_run)
        supervisor.check_health()
        failed_run.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        assert account.pk not in supervisor.processes
        assert failed_run.stop_reason == MinerRun.StopReason.UNEXPECTED_EXIT
        assert failed_run.exit_code == 29
        assert MinerIncident.objects.filter(account=account, status="open").exists()
        assert state.current_run_id is None
        assert state.advisory_pid is None
        assert state.worker_id == ""
        assert state.stable_since is None
        assert "return code 29" in state.last_error
        assert state.next_retry_at == clock.now()

        assert supervisor._perform_due_recoveries() == 1
        assert account.pk in supervisor.processes
        assert supervisor.processes[account.pk].run_id != failed_run.pk
        assert len(factory.processes) == 2
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_five_rapid_recovery_failures_enter_degraded_periodic_retry(tmp_path, settings):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory, startup_grace_seconds=10)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        clock.advance(10)
        supervisor.run_once(force_checks=True)

        # Original healthy run exits, then each of five rapid replacements exits
        # before its startup grace period can confirm it.
        factory.processes[-1].crash()
        supervisor.run_once(force_checks=True)
        for _ in range(5):
            factory.processes[-1].crash()
            supervisor.run_once(force_checks=True)

        state = MinerInstanceState.objects.get(account=account)
        incident = MinerIncident.objects.get(account=account, status=MinerIncident.Status.OPEN)
        assert state.desired_state == MinerInstanceState.DesiredState.RUNNING
        assert state.observed_state == MinerInstanceState.ObservedState.DEGRADED
        assert state.retry_count == 5
        assert state.next_retry_at == clock.now() + timedelta(seconds=100)
        assert RestartAttempt.objects.filter(
            incident=incident,
            outcome=RestartAttempt.Outcome.FAILED,
        ).count() == 5
        assert RestartAttempt.objects.filter(
            incident=incident,
            attempt_number=6,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        ).exists()

        count_before = len(factory.processes)
        clock.advance(99)
        supervisor.run_once(force_checks=True)
        assert len(factory.processes) == count_before
        clock.advance(1)
        supervisor.run_once(force_checks=True)
        assert len(factory.processes) == count_before + 1
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_startup_rejects_live_lease_and_recovers_stale_pid(tmp_path, settings):
    configure_farm()
    account = MinerAccount.objects.get(config_key="primary")
    state = MinerInstanceState.objects.get(account=account)
    state.desired_state = MinerInstanceState.DesiredState.RUNNING
    stale_run = create_launch_snapshot(account, worker_id="dead-worker")
    state.current_run = stale_run
    state.advisory_pid = 424242
    state.worker_id = "dead-worker"
    state.observed_state = MinerInstanceState.ObservedState.RUNNING
    state.save()

    clock = FakeClock()
    WorkerLease.objects.create(
        name="miner-supervisor",
        owner_id="dead-worker",
        pid=111,
        acquired_at=clock.now() - timedelta(minutes=5),
        heartbeat_at=clock.now() - timedelta(minutes=5),
        expires_at=clock.now() - timedelta(seconds=1),
    )
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        stale_run.refresh_from_db()
        state.refresh_from_db()
        assert stale_run.ended_at is not None
        assert stale_run.stop_reason == MinerRun.StopReason.UNEXPECTED_EXIT
        assert state.advisory_pid == factory.processes[0].pid
        assert state.advisory_pid != 424242
        assert MinerIncident.objects.filter(
            kind=MinerIncident.Kind.UNCLEAN_SUPERVISOR,
            status=MinerIncident.Status.RECOVERED,
        ).exists()
    finally:
        supervisor.shutdown()

    WorkerLease.objects.create(
        name="miner-supervisor",
        owner_id="another-live-worker",
        expires_at=clock.now() + timedelta(minutes=1),
        acquired_at=clock.now(),
        heartbeat_at=clock.now(),
    )
    rejected = make_supervisor(clock, ProcessFactory())
    with pytest.raises(SupervisorAlreadyRunning):
        rejected.startup()


@pytest.mark.django_db
def test_invalid_new_channel_config_keeps_healthy_process(tmp_path, settings):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        original = factory.processes[0]

        set_default_channels(())
        supervisor.reconcile_fingerprints()
        state = MinerInstanceState.objects.get(account=account)
        assert original.poll() is None
        assert len(factory.processes) == 1
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert "Configuration check failed" in state.last_error
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_database_reconciliation_starts_active_and_stops_archived_accounts(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        primary = MinerAccount.objects.get(config_key="primary")
        enqueue_command(primary, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)

        secondary = upsert_account(
            "secondary",
            username="SecondaryUser",
            password="secondary-secret",
        )
        secondary.runtime_state.desired_state = MinerInstanceState.DesiredState.RUNNING
        secondary.runtime_state.save(update_fields=("desired_state", "updated_at"))
        supervisor.reconcile_fingerprints()
        secondary.runtime_state.refresh_from_db()
        assert secondary.runtime_state.desired_state == MinerInstanceState.DesiredState.RUNNING
        assert secondary.pk in supervisor.processes

        primary.is_active = False
        primary.save(update_fields=("is_active", "updated_at"))
        primary.runtime_state.desired_state = MinerInstanceState.DesiredState.STOPPED
        primary.runtime_state.save(update_fields=("desired_state", "updated_at"))
        supervisor.reconcile_fingerprints()
        primary.refresh_from_db()
        primary.runtime_state.refresh_from_db()
        assert primary.is_active is False
        assert primary.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert primary.pk not in supervisor.processes
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_archiving_closes_unowned_recovery_incident(tmp_path, settings):
    configure_farm()
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now() + timedelta(seconds=5)
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery was waiting when configuration changed.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=state.next_retry_at,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        account.is_active = False
        account.save(update_fields=("is_active", "updated_at"))
        state.desired_state = MinerInstanceState.DesiredState.STOPPED
        state.save(update_fields=("desired_state", "updated_at"))

        supervisor.reconcile_fingerprints()

        account.refresh_from_db()
        state.refresh_from_db()
        attempt.refresh_from_db()
        incident.refresh_from_db()
        assert account.is_active is False
        assert state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert state.observed_state == MinerInstanceState.ObservedState.STOPPED
        assert state.next_retry_at is None
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert "account is archived" in attempt.error
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
        assert "account is archived" in incident.details
        assert supervisor.processes == {}
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_linux_children_configure_parent_death_without_preexec_fn(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        monkeypatch.setattr("controller.miner_supervisor.sys.platform", "linux")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        assert "preexec_fn" not in factory.options[0]
        assert factory.options[0]["env"]["TWITCH_FARM_SUPERVISOR_PID"] == str(os.getpid())
    finally:
        supervisor.shutdown()


def test_runner_configures_linux_parent_death_signal_after_exec(monkeypatch):
    parent_pid = 4242
    prctl_calls = []
    kill_calls = []

    class FakeLibc:
        def prctl(self, option, death_signal):
            prctl_calls.append((option, death_signal))
            return 0

    monkeypatch.setattr(miner_runner.sys, "platform", "linux")
    monkeypatch.setenv("TWITCH_FARM_SUPERVISOR_PID", str(parent_pid))
    monkeypatch.setattr(miner_runner.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(miner_runner.os, "getppid", lambda: parent_pid + 1)
    monkeypatch.setattr(miner_runner.os, "getpid", lambda: 5252)
    monkeypatch.setattr(
        miner_runner.os,
        "kill",
        lambda pid, death_signal: kill_calls.append((pid, death_signal)),
    )

    miner_runner.configure_linux_parent_death_signal()

    assert prctl_calls == [(1, signal.SIGKILL)]
    assert kill_calls == [(5252, signal.SIGKILL)]
    assert "TWITCH_FARM_SUPERVISOR_PID" not in os.environ


@pytest.mark.django_db
def test_runner_and_fake_miner_read_exact_snapshot_without_persisting_password(
    tmp_path,
    settings,
):
    configure_farm()
    account = MinerAccount.objects.get(config_key="primary")
    run = create_launch_snapshot(account, worker_id="test-worker")

    payload = load_launch_payload(run.pk, account.pk)
    assert payload.username == "PrimaryUser"
    assert payload.password == "never-put-this-in-argv"
    assert payload.channels == ("Alpha", "Beta")
    assert payload.password not in repr(payload)
    assert "password" not in {field.name for field in MinerRun._meta.get_fields()}

    record_path = tmp_path / "fake-miner.jsonl"
    call_command(
        "run_fake_miner",
        run.pk,
        account.pk,
        duration=0,
        record_file=str(record_path),
        verbosity=0,
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["run_id"] == run.pk
    assert record["account_key"] == account.config_key
    assert record["channels"] == ["Alpha", "Beta"]
    assert "password" not in record


@pytest.mark.django_db
def test_runtime_cookie_consumes_encrypted_seed_into_private_writable_directory(
    tmp_path,
    settings,
    monkeypatch,
):
    account = configure_farm()
    cookies = [
        {"name": "auth-token", "value": "session-secret"},
        {"name": "login", "value": "PrimaryUser"},
    ]
    AccountSessionSeed.objects.create(
        account=account,
        payload_ciphertext=encrypt_json(cookies),
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.chdir(runtime_dir)

    destination = prepare_runtime_cookie("PrimaryUser", account.pk)
    assert destination == runtime_dir / "cookies" / "PrimaryUser.pkl"
    assert pickle.loads(destination.read_bytes()) == cookies
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert not AccountSessionSeed.objects.filter(account=account).exists()

    destination.write_bytes(b"refreshed-cookie")
    AccountSessionSeed.objects.create(
        account=account,
        payload_ciphertext=encrypt_json(
            [{"name": "auth-token", "value": "replacement-secret"}]
        ),
    )
    replacement = prepare_runtime_cookie("PrimaryUser", account.pk)
    assert pickle.loads(replacement.read_bytes()) == [
        {"name": "auth-token", "value": "replacement-secret"}
    ]
    assert not AccountSessionSeed.objects.filter(account=account).exists()


def test_miner_entrypoint_suppresses_password_from_upstream_errors(monkeypatch):
    password = "upstream-error-password-secret"
    payload = LaunchPayload(
        username="PrimaryUser",
        password=password,
        channels=("Alpha",),
    )
    monkeypatch.setattr(miner_runner, "load_launch_payload", lambda *_args: payload)
    monkeypatch.setattr(miner_runner, "prepare_runtime_cookie", lambda *_args: None)

    def fail_with_password(_payload):
        raise RuntimeError(f"login failed for password={password}")

    monkeypatch.setattr(miner_runner, "run_miner", fail_with_password)

    with pytest.raises(RuntimeError) as captured:
        miner_runner.main(1, 1)

    assert password not in str(captured.value)
    assert "sensitive authentication details were suppressed" in str(captured.value)


@pytest.mark.django_db
def test_spawn_persistence_failure_terminates_child_before_losing_handle(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        run = create_launch_snapshot(account, worker_id=supervisor.worker_id)
        original_save = MinerInstanceState.save

        def fail_starting_save(instance, *args, **kwargs):
            if instance.observed_state == MinerInstanceState.ObservedState.STARTING:
                raise OperationalError("simulated post-spawn database failure")
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(MinerInstanceState, "save", fail_starting_save)
        with pytest.raises(OperationalError):
            supervisor._spawn_snapshot(run)

        process = factory.processes[0]
        run.refresh_from_db()
        assert process.poll() == -15
        assert account.pk not in supervisor.processes
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_runtime_directory_failure_finalizes_unspawned_run(tmp_path, settings):
    settings.TWITCH_FARM_LOG_FILE = tmp_path / "logs" / "twitch-farm.log"
    settings.TWITCH_FARM_LOG_WRITER = True
    configure_farm()
    invalid_runtime = tmp_path / "runtime-is-a-file"
    invalid_runtime.write_text("not a directory", encoding="utf-8")
    settings.TWITCH_FARM_RUNTIME_DIR = invalid_runtime
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        run = create_launch_snapshot(account, worker_id=supervisor.worker_id)

        with pytest.raises(FileExistsError):
            supervisor._spawn_snapshot(run)

        run.refresh_from_db()
        assert factory.processes == []
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
        assert run.ended_at is not None
        assert supervisor.pending_run_finalizations == {}
        contents = "\n".join(
            read_run_log_page(account_id=account.pk, run_id=run.pk)["lines"]
        )
        assert "start_failed" in contents
        assert "final_exit" in contents
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_unspawned_run_finalization_failure_is_retained_and_retried(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    invalid_runtime = tmp_path / "runtime-is-a-file"
    invalid_runtime.write_text("not a directory", encoding="utf-8")
    settings.TWITCH_FARM_RUNTIME_DIR = invalid_runtime
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        run = create_launch_snapshot(account, worker_id=supervisor.worker_id)
        original_close_run = supervisor._close_run

        def fail_close_run(*args, **kwargs):
            raise OperationalError("simulated finalization database failure")

        monkeypatch.setattr(supervisor, "_close_run", fail_close_run)
        with pytest.raises(FileExistsError):
            supervisor._spawn_snapshot(run)

        run.refresh_from_db()
        assert run.ended_at is None
        assert run.pk in supervisor.pending_run_finalizations

        monkeypatch.setattr(supervisor, "_close_run", original_close_run)
        assert supervisor._flush_pending_run_finalizations(account_id=account.pk) == 0
        run.refresh_from_db()
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
        assert run.ended_at is not None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_due_recovery_waits_for_pending_run_finalization(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        leaked_run = create_launch_snapshot(account, worker_id=supervisor.worker_id)
        supervisor.pending_run_finalizations[leaked_run.pk] = (
            account.pk,
            None,
            MinerRun.StopReason.START_FAILED,
            "pending finalization",
        )
        incident = MinerIncident.objects.create(
            account=account,
            run=leaked_run,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Waiting for recovery.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        original_close_run = supervisor._close_run

        def fail_close_run(*args, **kwargs):
            raise OperationalError("simulated finalization database failure")

        monkeypatch.setattr(supervisor, "_close_run", fail_close_run)
        assert supervisor._perform_due_recoveries() == 0
        attempt.refresh_from_db()
        assert attempt.outcome == RestartAttempt.Outcome.SCHEDULED
        assert factory.processes == []
        assert MinerRun.objects.count() == 1

        monkeypatch.setattr(supervisor, "_close_run", original_close_run)
        assert supervisor._perform_due_recoveries() == 1
        leaked_run.refresh_from_db()
        assert leaked_run.ended_at is not None
        assert len(factory.processes) == 1
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_recovery_claim_failure_leaves_attempt_scheduled_and_state_due(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Waiting for recovery.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        original_save = RestartAttempt.save

        def fail_started_save(instance, *args, **kwargs):
            if kwargs.get("update_fields") == ("started_at", "outcome"):
                raise OperationalError("simulated recovery claim failure")
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(RestartAttempt, "save", fail_started_save)
        assert supervisor._perform_due_recoveries() == 0

        state.refresh_from_db()
        attempt.refresh_from_db()
        assert state.retry_count == 0
        assert state.next_retry_at == clock.now()
        assert state.observed_state == MinerInstanceState.ObservedState.RESTARTING
        assert attempt.outcome == RestartAttempt.Outcome.SCHEDULED
        assert attempt.started_at is None
        assert MinerRun.objects.count() == 0
        assert supervisor.processes == {}
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_recovery_run_association_failure_finalizes_snapshot_before_reschedule(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Waiting for recovery.",
        )
        attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        original_save = RestartAttempt.save

        def fail_run_association(instance, *args, **kwargs):
            if kwargs.get("update_fields") == ("run",):
                raise OperationalError("simulated recovery run association failure")
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(RestartAttempt, "save", fail_run_association)
        assert supervisor._perform_due_recoveries() == 0

        state.refresh_from_db()
        attempt.refresh_from_db()
        leaked_candidate = MinerRun.objects.get(account=account)
        assert leaked_candidate.stop_reason == MinerRun.StopReason.START_FAILED
        assert leaked_candidate.ended_at is not None
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert RestartAttempt.objects.filter(
            incident=incident,
            attempt_number=2,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        ).exists()
        assert state.next_retry_at == clock.now()
        assert supervisor.pending_run_finalizations == {}
        assert supervisor.processes == {}
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_cleanup_reconciliation_reuses_already_scheduled_recovery_attempt(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory(fail_terminate=True)
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.RESTARTING
        state.next_retry_at = clock.now()
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Waiting for recovery.",
        )
        first_attempt = RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        original_save = MinerInstanceState.save
        fail_starting_save = True

        def fail_first_starting_save(instance, *args, **kwargs):
            nonlocal fail_starting_save
            if (
                fail_starting_save
                and instance.observed_state == MinerInstanceState.ObservedState.STARTING
            ):
                fail_starting_save = False
                raise OperationalError("simulated post-spawn database failure")
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(MinerInstanceState, "save", fail_first_starting_save)
        assert supervisor._perform_due_recoveries() == 0

        managed = supervisor.processes[account.pk]
        first_attempt.refresh_from_db()
        state.refresh_from_db()
        successor = RestartAttempt.objects.get(
            incident=incident,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        )
        assert managed.cleanup_required is True
        assert first_attempt.outcome == RestartAttempt.Outcome.FAILED
        assert successor.attempt_number == 2
        assert state.retry_count == 1
        assert state.next_retry_at == successor.scheduled_at

        managed.process.fail_terminate = False
        supervisor.check_health()

        state.refresh_from_db()
        pending = list(
            RestartAttempt.objects.filter(
                incident=incident,
                outcome=RestartAttempt.Outcome.SCHEDULED,
            )
        )
        assert account.pk not in supervisor.processes
        assert [attempt.pk for attempt in pending] == [successor.pk]
        assert not RestartAttempt.objects.filter(
            incident=incident,
            attempt_number=3,
        ).exists()
        assert state.current_run_id is None
        assert state.advisory_pid is None
        assert state.worker_id == ""
        assert state.stable_since is None
        assert "post-spawn database failure" in state.last_error
        assert state.retry_count == 1
        assert state.next_retry_at == successor.scheduled_at

        factory.fail_terminate = False
        assert supervisor._perform_due_recoveries() == 1
        successor.refresh_from_db()
        state.refresh_from_db()
        assert successor.outcome == RestartAttempt.Outcome.STARTED
        assert state.retry_count == 2
        assert len(factory.processes) == 2

        supervisor.check_health()
        successor.refresh_from_db()
        incident.refresh_from_db()
        assert successor.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_reset_retry_sequence_allocates_new_incident_ordinal_and_recovers(
    tmp_path,
    settings,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        state = MinerInstanceState.objects.get(account=account)
        state.desired_state = MinerInstanceState.DesiredState.RUNNING
        state.observed_state = MinerInstanceState.ObservedState.DEGRADED
        state.retry_count = 0
        state.save()
        incident = MinerIncident.objects.create(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            status=MinerIncident.Status.OPEN,
            summary="Recovery sequence was reset.",
        )
        RestartAttempt.objects.create(
            incident=incident,
            attempt_number=1,
            scheduled_at=clock.now(),
            started_at=clock.now(),
            finished_at=clock.now(),
            outcome=RestartAttempt.Outcome.FAILED,
            error="Earlier failure before sequence reset.",
        )

        scheduled = supervisor._schedule_recovery(state, incident)
        state.refresh_from_db()
        assert scheduled.attempt_number == 2
        assert scheduled.outcome == RestartAttempt.Outcome.SCHEDULED
        assert state.retry_count == 0
        assert state.next_retry_at == clock.now()
        assert RestartAttempt.objects.filter(
            incident=incident,
            outcome=RestartAttempt.Outcome.SCHEDULED,
        ).count() == 1

        assert supervisor._perform_due_recoveries() == 1
        state.refresh_from_db()
        scheduled.refresh_from_db()
        assert state.retry_count == 1
        assert scheduled.outcome == RestartAttempt.Outcome.STARTED
        assert account.pk in supervisor.processes

        supervisor.check_health()
        incident.refresh_from_db()
        scheduled.refresh_from_db()
        assert scheduled.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_failed_spawn_cleanup_is_never_promoted_and_retries_until_finalized(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory(fail_terminate=True)
    supervisor = make_supervisor(clock, factory, startup_grace_seconds=0)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        run = create_launch_snapshot(account, worker_id=supervisor.worker_id)
        original_save = MinerInstanceState.save

        def fail_starting_save(instance, *args, **kwargs):
            if instance.observed_state == MinerInstanceState.ObservedState.STARTING:
                raise OperationalError("simulated post-spawn database failure")
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(MinerInstanceState, "save", fail_starting_save)
        with pytest.raises(OperationalError):
            supervisor._spawn_snapshot(run)

        managed = supervisor.processes[account.pk]
        assert managed.cleanup_required is True
        supervisor.check_health()
        state = MinerInstanceState.objects.get(account=account)
        run.refresh_from_db()
        assert managed.confirmed is False
        assert state.observed_state == MinerInstanceState.ObservedState.DEGRADED
        assert run.ended_at is None

        managed.process.fail_terminate = False
        supervisor.check_health()
        state.refresh_from_db()
        run.refresh_from_db()
        assert account.pk not in supervisor.processes
        assert state.observed_state == MinerInstanceState.ObservedState.DEGRADED
        assert state.current_run_id is None
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_dead_spawn_cleanup_keeps_tombstone_until_run_finalization_commits(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory, startup_grace_seconds=0)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        run = create_launch_snapshot(account, worker_id=supervisor.worker_id)
        original_save = MinerInstanceState.save
        original_close_run = supervisor._close_run

        def fail_starting_save(instance, *args, **kwargs):
            if instance.observed_state == MinerInstanceState.ObservedState.STARTING:
                raise OperationalError("simulated post-spawn database failure")
            return original_save(instance, *args, **kwargs)

        def fail_close_run(*args, **kwargs):
            raise OperationalError("simulated run finalization failure")

        monkeypatch.setattr(MinerInstanceState, "save", fail_starting_save)
        monkeypatch.setattr(supervisor, "_close_run", fail_close_run)
        with pytest.raises(OperationalError):
            supervisor._spawn_snapshot(run)

        managed = supervisor.processes[account.pk]
        run.refresh_from_db()
        assert managed.process.poll() == -15
        assert managed.cleanup_required is True
        assert run.ended_at is None

        monkeypatch.setattr(supervisor, "_close_run", original_close_run)
        supervisor.check_health()
        run.refresh_from_db()
        assert account.pk not in supervisor.processes
        assert run.stop_reason == MinerRun.StopReason.START_FAILED
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_stop_signal_failure_retains_process_ownership_for_retry(tmp_path, settings):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        managed = supervisor.processes[account.pk]
        process = managed.process
        original_terminate = process.terminate

        def fail_terminate():
            raise OSError("simulated signal failure")

        process.terminate = fail_terminate
        MinerInstanceState.objects.filter(account=account).update(
            desired_state=MinerInstanceState.DesiredState.STOPPED
        )
        with pytest.raises(OSError):
            supervisor.stop_account(account)

        run = MinerRun.objects.get(pk=managed.run_id)
        state = MinerInstanceState.objects.get(account=account)
        assert supervisor.processes[account.pk] is managed
        assert process.poll() is None
        assert run.ended_at is None
        assert state.observed_state == MinerInstanceState.ObservedState.DEGRADED

        process.terminate = original_terminate
        supervisor.stop_account(account)
        assert account.pk not in supervisor.processes
        run.refresh_from_db()
        assert run.ended_at is not None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_new_start_finalizes_dead_stop_tombstone_before_replacement(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        managed = supervisor.processes[account.pk]
        run = MinerRun.objects.get(pk=managed.run_id)
        MinerInstanceState.objects.filter(account=account).update(
            desired_state=MinerInstanceState.DesiredState.STOPPED
        )
        original_close_run = supervisor._close_run

        def fail_close_run(*args, **kwargs):
            raise OperationalError("simulated run finalization failure")

        monkeypatch.setattr(supervisor, "_close_run", fail_close_run)
        with pytest.raises(OperationalError):
            supervisor.stop_account(account)

        run.refresh_from_db()
        assert managed.process.poll() == -15
        assert supervisor.processes[account.pk] is managed
        assert managed.pending_stop_reason == MinerRun.StopReason.ADMIN_STOP
        assert run.ended_at is None

        monkeypatch.setattr(supervisor, "_close_run", original_close_run)
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        run.refresh_from_db()
        assert account.pk in supervisor.processes
        assert supervisor.processes[account.pk].run_id != run.pk
        assert run.stop_reason == MinerRun.StopReason.ADMIN_STOP
        state = MinerInstanceState.objects.get(account=account)
        assert state.current_run_id != run.pk
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert not MinerIncident.objects.filter(account=account).exists()
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_health_retry_preserves_planned_restart_reason_after_finalization_failure(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        old_managed = supervisor.processes[account.pk]
        old_run = MinerRun.objects.get(pk=old_managed.run_id)
        original_close_run = supervisor._close_run
        failed_once = False

        def fail_old_run_once(run_id, *args, **kwargs):
            nonlocal failed_once
            if run_id == old_run.pk and not failed_once:
                failed_once = True
                raise OperationalError("simulated planned-stop finalization failure")
            return original_close_run(run_id, *args, **kwargs)

        monkeypatch.setattr(supervisor, "_close_run", fail_old_run_once)
        command = enqueue_command(account, MinerCommand.Action.RESTART)
        assert supervisor.process_pending_commands() == 1

        command.refresh_from_db()
        old_run.refresh_from_db()
        assert command.status == MinerCommand.Status.FAILED
        assert old_managed.process.poll() == -15
        assert old_managed.pending_stop_reason == MinerRun.StopReason.ADMIN_RESTART
        assert old_run.ended_at is None
        assert supervisor.processes[account.pk] is old_managed

        monkeypatch.setattr(supervisor, "_close_run", original_close_run)
        supervisor.check_health()
        supervisor.check_health()

        old_run.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        assert old_run.stop_reason == MinerRun.StopReason.ADMIN_RESTART
        assert state.desired_state == MinerInstanceState.DesiredState.RUNNING
        assert state.current_run_id != old_run.pk
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert supervisor.processes[account.pk].run_id != old_run.pk
        assert not MinerIncident.objects.filter(
            account=account,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
        ).exists()
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_crash_detected_during_admin_stop_remains_an_incident(tmp_path, settings):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        failed_run = MinerInstanceState.objects.get(account=account).current_run
        factory.processes[0].crash(42)

        stop = enqueue_command(account, MinerCommand.Action.STOP)
        supervisor.run_once(force_checks=True)

        stop.refresh_from_db()
        failed_run.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        incident = MinerIncident.objects.get(account=account)
        assert stop.status == MinerCommand.Status.SUCCEEDED
        assert failed_run.stop_reason == MinerRun.StopReason.UNEXPECTED_EXIT
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
        assert state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert state.observed_state == MinerInstanceState.ObservedState.STOPPED
        assert account.pk not in supervisor.processes
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_startup_fails_recovery_attempt_interrupted_by_worker_death(tmp_path, settings):
    configure_farm()
    account = MinerAccount.objects.get(config_key="primary")
    state = MinerInstanceState.objects.get(account=account)
    state.desired_state = MinerInstanceState.DesiredState.RUNNING
    stale_run = create_launch_snapshot(account, worker_id="dead-worker")
    state.current_run = stale_run
    state.advisory_pid = 12345
    state.worker_id = "dead-worker"
    state.observed_state = MinerInstanceState.ObservedState.RESTARTING
    state.save()
    incident = MinerIncident.objects.create(
        account=account,
        run=stale_run,
        kind=MinerIncident.Kind.UNEXPECTED_EXIT,
        status=MinerIncident.Status.OPEN,
        summary="Recovery was in progress.",
    )
    attempt = RestartAttempt.objects.create(
        incident=incident,
        run=stale_run,
        attempt_number=1,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        outcome=RestartAttempt.Outcome.STARTED,
    )

    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        attempt.refresh_from_db()
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert attempt.finished_at is not None
        assert "supervisor startup reconciliation" in attempt.error

        supervisor.run_once(force_checks=True)
        incident.refresh_from_db()
        successor = RestartAttempt.objects.get(incident=incident, attempt_number=2)
        state.refresh_from_db()
        assert incident.status == MinerIncident.Status.RECOVERED
        assert successor.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert successor.run_id == state.current_run_id
        assert successor.finished_at is not None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_startup_resolves_scheduled_attempt_before_direct_reconciliation(
    tmp_path,
    settings,
):
    configure_farm()
    account = MinerAccount.objects.get(config_key="primary")
    state = MinerInstanceState.objects.get(account=account)
    state.desired_state = MinerInstanceState.DesiredState.RUNNING
    state.next_retry_at = timezone.now() + timedelta(minutes=5)
    state.observed_state = MinerInstanceState.ObservedState.RESTARTING
    state.save()
    incident = MinerIncident.objects.create(
        account=account,
        kind=MinerIncident.Kind.UNEXPECTED_EXIT,
        status=MinerIncident.Status.OPEN,
        summary="Waiting for recovery backoff.",
    )
    attempt = RestartAttempt.objects.create(
        incident=incident,
        attempt_number=1,
        scheduled_at=state.next_retry_at,
        outcome=RestartAttempt.Outcome.SCHEDULED,
    )

    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    try:
        attempt.refresh_from_db()
        assert attempt.outcome == RestartAttempt.Outcome.FAILED
        assert attempt.finished_at is not None
        assert "supervisor startup reconciliation" in attempt.error

        supervisor.run_once(force_checks=True)
        incident.refresh_from_db()
        successor = RestartAttempt.objects.get(incident=incident, attempt_number=2)
        state.refresh_from_db()
        assert incident.status == MinerIncident.Status.RECOVERED
        assert successor.outcome == RestartAttempt.Outcome.SUCCEEDED
        assert successor.run_id == state.current_run_id
        assert successor.finished_at is not None
        assert not RestartAttempt.objects.filter(
            incident=incident,
            outcome__in=(
                RestartAttempt.Outcome.SCHEDULED,
                RestartAttempt.Outcome.STARTED,
            ),
        ).exists()
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "action",
    (MinerCommand.Action.START, MinerCommand.Action.RESTART),
)
def test_startup_queued_launch_takes_over_recovery_without_active_attempt_leak(
    tmp_path,
    settings,
    action,
):
    configure_farm()
    account = MinerAccount.objects.get(config_key="primary")
    state = MinerInstanceState.objects.get(account=account)
    state.desired_state = MinerInstanceState.DesiredState.RUNNING
    state.observed_state = MinerInstanceState.ObservedState.RESTARTING
    state.next_retry_at = timezone.now() + timedelta(minutes=5)
    state.save()
    incident = MinerIncident.objects.create(
        account=account,
        kind=MinerIncident.Kind.UNEXPECTED_EXIT,
        status=MinerIncident.Status.OPEN,
        summary="Recovery was waiting when the worker stopped.",
    )
    interrupted = RestartAttempt.objects.create(
        incident=incident,
        attempt_number=1,
        scheduled_at=state.next_retry_at,
        outcome=RestartAttempt.Outcome.SCHEDULED,
    )
    command = enqueue_command(account, action)

    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        command.refresh_from_db()
        interrupted.refresh_from_db()
        successor = RestartAttempt.objects.get(incident=incident, attempt_number=2)
        assert command.status == MinerCommand.Status.LEASED
        assert interrupted.outcome == RestartAttempt.Outcome.FAILED
        assert successor.outcome == RestartAttempt.Outcome.FAILED
        assert f"explicit {action} command" in successor.error
        assert not RestartAttempt.objects.filter(
            incident=incident,
            outcome__in=(
                RestartAttempt.Outcome.SCHEDULED,
                RestartAttempt.Outcome.STARTED,
            ),
        ).exists()
        assert len(factory.processes) == 1

        supervisor.run_once(force_checks=True)

        command.refresh_from_db()
        incident.refresh_from_db()
        assert command.status == MinerCommand.Status.SUCCEEDED
        assert incident.status == MinerIncident.Status.RECOVERED
        assert incident.recovered_at is not None
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_older_leased_start_cannot_overwrite_concurrent_stop(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        leased_start = supervisor._lease_next_command()
        assert leased_start is not None

        def concurrent_stop(_command):
            enqueue_command(account, MinerCommand.Action.STOP)
            return False

        monkeypatch.setattr(supervisor, "_is_superseded", concurrent_stop)
        supervisor.execute_command(leased_start)

        leased_start.refresh_from_db()
        state = MinerInstanceState.objects.get(account=account)
        assert leased_start.status == MinerCommand.Status.CANCELLED
        assert state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert factory.processes == []
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "shutdown_failure",
    (
        OperationalError("simulated first-child shutdown failure"),
        KeyboardInterrupt(),
    ),
)
def test_shutdown_continues_after_child_failure_and_releases_singleton_ownership(
    tmp_path,
    settings,
    monkeypatch,
    shutdown_failure,
):
    configure_farm(include_second=True)
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = MinerSupervisor(
        options=runtime_options(lock_path=tmp_path / "worker.lock"),
        process_factory=factory,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=lambda seconds: None,
        worker_id="test-worker",
        use_file_lock=True,
    )
    supervisor.startup()

    primary = MinerAccount.objects.get(config_key="primary")
    secondary = MinerAccount.objects.get(config_key="secondary")
    enqueue_command(primary, MinerCommand.Action.START)
    enqueue_command(secondary, MinerCommand.Action.START)
    supervisor.run_once(force_checks=True)
    assert len(factory.processes) == 2
    assert WorkerLease.objects.filter(owner_id=supervisor.worker_id).exists()
    assert supervisor._lock_handle is not None

    original_stop = supervisor.stop_account
    stop_attempts: list[int] = []
    failed_once = False

    def fail_first_stop(account, **kwargs):
        nonlocal failed_once
        stop_attempts.append(account.pk)
        if account.pk == primary.pk and not failed_once:
            failed_once = True
            raise shutdown_failure
        return original_stop(account, **kwargs)

    original_release = supervisor._release_file_lock
    lock_release_attempted = False

    def record_lock_release() -> None:
        nonlocal lock_release_attempted
        lock_release_attempted = True
        original_release()

    monkeypatch.setattr(supervisor, "stop_account", fail_first_stop)
    monkeypatch.setattr(supervisor, "_release_file_lock", record_lock_release)

    with pytest.raises(type(shutdown_failure)):
        supervisor.shutdown()

    assert stop_attempts == [primary.pk, secondary.pk]
    assert primary.pk in supervisor.processes
    assert secondary.pk not in supervisor.processes
    assert not factory.processes[0].terminated
    assert factory.processes[1].terminated
    assert not WorkerLease.objects.filter(owner_id=supervisor.worker_id).exists()
    assert lock_release_attempted
    assert supervisor._lock_handle is None
    assert not supervisor._started
    assert supervisor._shutdown_incomplete

    supervisor.shutdown()

    assert stop_attempts == [primary.pk, secondary.pk]
    assert supervisor.processes == {}
    assert factory.processes[0].terminated
    assert not supervisor._shutdown_incomplete


@pytest.mark.django_db
def test_shutdown_retries_a_transient_database_lease_deletion_failure(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    supervisor = make_supervisor(clock, ProcessFactory())
    supervisor.startup()
    assert WorkerLease.objects.filter(owner_id=supervisor.worker_id).exists()

    original_filter = WorkerLease.objects.filter
    fail_delete_once = True

    class DeleteOnceFailure:
        def delete(self):
            nonlocal fail_delete_once
            fail_delete_once = False
            raise OperationalError("simulated lease deletion failure")

    def flaky_filter(*args, **kwargs):
        queryset = original_filter(*args, **kwargs)
        return DeleteOnceFailure() if fail_delete_once else queryset

    monkeypatch.setattr(WorkerLease.objects, "filter", flaky_filter)

    with pytest.raises(OperationalError, match="simulated lease deletion failure"):
        supervisor.shutdown()

    assert WorkerLease.objects.filter(owner_id=supervisor.worker_id).exists()
    assert supervisor._shutdown_incomplete
    assert not supervisor._started

    supervisor.shutdown()

    assert not WorkerLease.objects.filter(owner_id=supervisor.worker_id).exists()
    assert not supervisor._shutdown_incomplete


@pytest.mark.django_db
def test_released_shutdown_retry_cannot_clear_a_new_supervisors_run(
    tmp_path,
    settings,
    monkeypatch,
):
    configure_farm()
    clock = FakeClock()
    lock_path = tmp_path / "worker.lock"
    old_factory = ProcessFactory()
    old_supervisor = MinerSupervisor(
        options=runtime_options(lock_path=lock_path),
        process_factory=old_factory,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=lambda seconds: None,
        worker_id="old-worker",
        use_file_lock=True,
    )
    old_supervisor.startup()
    account = MinerAccount.objects.get(config_key="primary")
    enqueue_command(account, MinerCommand.Action.START)
    old_supervisor.run_once(force_checks=True)
    old_managed = old_supervisor.processes[account.pk]
    old_run_id = old_managed.run_id

    original_stop = old_supervisor.stop_account
    fail_once = True

    def transient_stop_failure(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OperationalError("simulated shutdown handoff failure")
        return original_stop(*args, **kwargs)

    monkeypatch.setattr(old_supervisor, "stop_account", transient_stop_failure)
    with pytest.raises(OperationalError, match="simulated shutdown handoff failure"):
        old_supervisor.shutdown()

    assert account.pk in old_supervisor.processes
    assert old_managed.process.poll() is None
    assert not WorkerLease.objects.filter(owner_id="old-worker").exists()

    new_factory = ProcessFactory()
    new_supervisor = MinerSupervisor(
        options=runtime_options(lock_path=lock_path),
        process_factory=new_factory,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=lambda seconds: None,
        worker_id="new-worker",
        use_file_lock=True,
    )
    new_supervisor.startup()
    try:
        new_managed = new_supervisor.processes[account.pk]
        assert new_managed.run_id != old_run_id
        assert new_managed.process.poll() is None

        old_supervisor.shutdown()

        state = MinerInstanceState.objects.get(account=account)
        assert old_supervisor.processes == {}
        assert old_managed.process.poll() is not None
        assert state.worker_id == "new-worker"
        assert state.current_run_id == new_managed.run_id
        assert new_managed.process.poll() is None
        assert WorkerLease.objects.filter(owner_id="new-worker").exists()
    finally:
        new_supervisor.shutdown()
