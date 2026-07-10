from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from controller.miner_runner import load_launch_payload, prepare_runtime_cookie
from controller.miner_supervisor import (
    MinerSupervisor,
    SupervisorAlreadyRunning,
    SupervisorOptions,
)
from controller.models import (
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    RestartAttempt,
    WorkerLease,
)
from controller.services import create_launch_snapshot, enqueue_command, sync_config_accounts


def write_config(path: Path, *, channels: tuple[str, ...] = ("Alpha", "Beta")) -> Path:
    channel_yaml = "\n".join(f"  - {channel}" for channel in channels)
    path.write_text(
        f"""settings:
  autostart_instances: false
twitch_users:
  primary:
    username: PrimaryUser
    password: never-put-this-in-argv
default_channels:
{channel_yaml or '  []'}
""",
        encoding="utf-8",
    )
    return path


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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
        assert argv[-2:] == [str(run.pk), "primary"]
        assert "never-put-this-in-argv" not in " ".join(argv)
        assert "Alpha" not in argv and "Beta" not in argv
        assert factory.options[0]["cwd"] == settings.TWITCH_FARM_RUNTIME_DIR
        assert str(settings.BASE_DIR) in factory.options[0]["env"]["PYTHONPATH"]

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

        clock.advance(1000)
        supervisor.run_once(force_checks=True)
        assert len(factory.processes) == 1
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_unexpected_exit_records_incident_attempt_and_recovery(tmp_path, settings):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_five_rapid_recovery_failures_enter_degraded_periodic_retry(tmp_path, settings):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
    sync_config_accounts()
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
    config_path = write_config(tmp_path / "config.yaml")
    settings.TWITCH_FARM_CONFIG = config_path
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        original = factory.processes[0]

        write_config(config_path, channels=())
        supervisor.reconcile_fingerprints()
        state = MinerInstanceState.objects.get(account=account)
        assert original.poll() is None
        assert len(factory.processes) == 1
        assert state.observed_state == MinerInstanceState.ObservedState.RUNNING
        assert "Configuration check failed" in state.last_error
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_periodic_config_sync_adds_and_removes_accounts(tmp_path, settings):
    config_path = write_config(tmp_path / "config.yaml")
    settings.TWITCH_FARM_CONFIG = config_path
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        primary = MinerAccount.objects.get(config_key="primary")
        enqueue_command(primary, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)

        config_path.write_text(
            """settings:
  autostart_instances: true
twitch_users:
  primary:
    username: PrimaryUser
    password: primary-secret
  secondary:
    username: SecondaryUser
    password: secondary-secret
default_channels:
  - Alpha
""",
            encoding="utf-8",
        )
        supervisor.reconcile_fingerprints()
        secondary = MinerAccount.objects.get(config_key="secondary")
        assert secondary.runtime_state.desired_state == MinerInstanceState.DesiredState.RUNNING
        assert secondary.pk in supervisor.processes

        config_path.write_text(
            """settings:
  autostart_instances: true
twitch_users:
  secondary:
    username: SecondaryUser
    password: secondary-secret
default_channels:
  - Alpha
""",
            encoding="utf-8",
        )
        supervisor.reconcile_fingerprints()
        primary.refresh_from_db()
        primary.runtime_state.refresh_from_db()
        assert primary.is_configured is False
        assert primary.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED
        assert primary.pk not in supervisor.processes
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_linux_children_receive_parent_death_signal_hook(tmp_path, settings, monkeypatch):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
    clock = FakeClock()
    factory = ProcessFactory()
    supervisor = make_supervisor(clock, factory)
    supervisor.startup()
    try:
        account = MinerAccount.objects.get(config_key="primary")
        monkeypatch.setattr("controller.miner_supervisor.sys.platform", "linux")
        enqueue_command(account, MinerCommand.Action.START)
        supervisor.run_once(force_checks=True)
        assert callable(factory.options[0]["preexec_fn"])
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_runner_and_fake_miner_read_exact_snapshot_without_persisting_password(
    tmp_path,
    settings,
):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
    sync_config_accounts()
    account = MinerAccount.objects.get(config_key="primary")
    run = create_launch_snapshot(account, worker_id="test-worker")

    payload = load_launch_payload(run.pk, account.config_key)
    assert payload.username == "PrimaryUser"
    assert payload.password == "never-put-this-in-argv"
    assert payload.channels == ("Alpha", "Beta")
    assert "password" not in {field.name for field in MinerRun._meta.get_fields()}

    record_path = tmp_path / "fake-miner.jsonl"
    call_command(
        "run_fake_miner",
        run.pk,
        account.config_key,
        duration=0,
        record_file=str(record_path),
        verbosity=0,
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["run_id"] == run.pk
    assert record["account_key"] == account.config_key
    assert record["channels"] == ["Alpha", "Beta"]
    assert "password" not in record


def test_runtime_cookie_is_seeded_into_private_writable_directory(
    tmp_path,
    settings,
    monkeypatch,
):
    seed_dir = tmp_path / "seed-cookies"
    seed_dir.mkdir()
    seed = seed_dir / "PrimaryUser.pkl"
    seed.write_bytes(b"seed-cookie")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    settings.TWITCH_FARM_COOKIES_DIR = seed_dir
    monkeypatch.chdir(runtime_dir)

    destination = prepare_runtime_cookie("PrimaryUser")
    assert destination == runtime_dir / "cookies" / "PrimaryUser.pkl"
    assert destination.read_bytes() == b"seed-cookie"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700

    destination.write_bytes(b"refreshed-cookie")
    assert prepare_runtime_cookie("PrimaryUser").read_bytes() == b"refreshed-cookie"


@pytest.mark.django_db
def test_spawn_persistence_failure_terminates_child_before_losing_handle(
    tmp_path,
    settings,
    monkeypatch,
):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_unspawned_run_finalization_failure_is_retained_and_retried(
    tmp_path,
    settings,
    monkeypatch,
):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
def test_failed_spawn_cleanup_is_never_promoted_and_retries_until_finalized(
    tmp_path,
    settings,
    monkeypatch,
):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
def test_crash_detected_during_admin_stop_remains_an_incident(tmp_path, settings):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
    sync_config_accounts()
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
        assert incident.status == MinerIncident.Status.RECOVERED
        assert not RestartAttempt.objects.filter(
            pk=attempt.pk,
            outcome=RestartAttempt.Outcome.STARTED,
        ).exists()
    finally:
        supervisor.shutdown()


@pytest.mark.django_db
def test_startup_resolves_scheduled_attempt_before_direct_reconciliation(
    tmp_path,
    settings,
):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
    sync_config_accounts()
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
        assert incident.status == MinerIncident.Status.RECOVERED
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
def test_older_leased_start_cannot_overwrite_concurrent_stop(
    tmp_path,
    settings,
    monkeypatch,
):
    settings.TWITCH_FARM_CONFIG = write_config(tmp_path / "config.yaml")
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
