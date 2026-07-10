from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from controller.apps import configure_sqlite_connection
from controller.config import FarmConfig, TwitchUserConfig, get_account_credentials, load_config
from controller.models import (
    AccountChannelSelection,
    ActionLog,
    ChannelPreset,
    MinerAccount,
    MinerCommand,
    MinerInstanceState,
    MinerRun,
)
from controller.services import (
    create_launch_snapshot,
    enqueue_command,
    normalize_channels,
    resolve_channels,
    save_preset,
    set_account_channel_selection,
    sync_config_accounts,
)


def make_config(tmp_path: Path, *, autostart: bool = False, include_second: bool = False) -> Path:
    second = """
  second:
    username: SecondUser
    password: second-secret
""" if include_second else ""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""settings:
  autostart_instances: {str(autostart).lower()}
twitch_users:
  primary:
    username: PrimaryUser
    password: super-secret
{second}default_channels:
  - Alpha
  - beta
  - ALPHA
""",
        encoding="utf-8",
    )
    return path


def test_sqlite_connection_pragmas_honor_configured_busy_timeout():
    statements: list[str] = []

    class RecordingCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement):
            statements.append(statement)

    class FakeConnection:
        vendor = "sqlite"
        settings_dict = {"OPTIONS": {"timeout": 7.25}}

        def cursor(self):
            return RecordingCursor()

    configure_sqlite_connection(sender=None, connection=FakeConnection())

    assert statements == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=FULL",
        "PRAGMA busy_timeout=7250",
    ]


@pytest.mark.django_db
def test_sync_uses_yaml_without_persisting_secrets_and_preserves_manual_stop(tmp_path):
    config_path = make_config(tmp_path, autostart=True)
    result = sync_config_accounts(config_path)

    assert result.created == ("primary",)
    account = MinerAccount.objects.get(config_key="primary")
    assert account.display_username == "PrimaryUser"
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.RUNNING
    assert "password" not in {field.name for field in MinerAccount._meta.get_fields()}
    assert "super-secret" not in repr(load_config(config_path))
    assert get_account_credentials("primary", config_path).password == "super-secret"

    account.runtime_state.desired_state = MinerInstanceState.DesiredState.STOPPED
    account.runtime_state.save(update_fields=("desired_state", "updated_at"))
    result = sync_config_accounts(config_path)
    account.runtime_state.refresh_from_db()
    assert result.created == ()
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED

    empty_config = FarmConfig(
        twitch_users={},
        default_channels=("alpha",),
        autostart_instances=True,
        path=config_path,
    )
    result = sync_config_accounts(empty_config)
    account.refresh_from_db()
    assert result.disabled == ("primary",)
    assert account.is_configured is False
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED


@pytest.mark.django_db
def test_channel_resolution_selection_restart_and_command_coalescing(tmp_path, settings):
    config_path = make_config(tmp_path)
    settings.TWITCH_FARM_CONFIG = config_path
    sync_config_accounts(config_path)
    account = MinerAccount.objects.get(config_key="primary")

    assert normalize_channels(" Alpha, beta\nALPHA ") == ["Alpha", "beta"]
    default = resolve_channels(account, config_path)
    assert default.channels == ("Alpha", "beta")
    assert default.mode == AccountChannelSelection.Mode.DEFAULT

    account.runtime_state.desired_state = MinerInstanceState.DesiredState.RUNNING
    account.runtime_state.save(update_fields=("desired_state", "updated_at"))
    selection = set_account_channel_selection(
        account,
        AccountChannelSelection.Mode.CUSTOM,
        channels="Gamma, gamma\ndelta",
    )
    assert selection.mode == AccountChannelSelection.Mode.CUSTOM
    assert list(account.custom_channels.values_list("name", flat=True)) == ["Gamma", "delta"]
    assert MinerCommand.objects.filter(action=MinerCommand.Action.RESTART).count() == 1

    first = enqueue_command(account, MinerCommand.Action.RESTART)
    second = enqueue_command(account, MinerCommand.Action.RESTART)
    assert first.pk == second.pk

    stop = enqueue_command(account, MinerCommand.Action.STOP)
    first.refresh_from_db()
    account.runtime_state.refresh_from_db()
    assert first.status == MinerCommand.Status.CANCELLED
    assert stop.status == MinerCommand.Status.QUEUED
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED


def test_sqlite_immediate_transactions_preserve_latest_concurrent_command(
    tmp_path,
):
    config_path = make_config(tmp_path)
    database_path = tmp_path / "concurrent.sqlite3"
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_DEBUG": "1",
            "DJANGO_SECRET_KEY": "concurrency-test-only",
            "TWITCH_FARM_CONFIG": str(config_path),
            "TWITCH_FARM_DB": str(database_path),
        }
    )
    migration = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput", "--verbosity", "0"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migration.returncode == 0, migration.stdout + migration.stderr

    # pytest-django's shared in-memory SQLite database uses SQLITE_LOCKED table
    # semantics that don't honor busy_timeout. Exercise the real file-backed
    # deployment mode in a child process instead.
    probe = textwrap.dedent(
        """
        import os
        from threading import Event, Thread

        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twitch_farm.settings")
        django.setup()

        from django.db import close_old_connections, transaction

        from controller.models import MinerAccount, MinerCommand, MinerInstanceState
        from controller.services import enqueue_command, sync_config_accounts

        sync_config_accounts()
        account_id = MinerAccount.objects.get(config_key="primary").pk
        first_has_written = Event()
        release_first = Event()
        second_started = Event()
        second_finished = Event()
        errors = []

        def queue_start_while_holding_transaction():
            close_old_connections()
            try:
                with transaction.atomic():
                    account = MinerAccount.objects.get(pk=account_id)
                    enqueue_command(account, MinerCommand.Action.START)
                    first_has_written.set()
                    if not release_first.wait(timeout=5):
                        raise TimeoutError("first transaction release timed out")
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                close_old_connections()

        def queue_later_stop():
            close_old_connections()
            second_started.set()
            try:
                account = MinerAccount.objects.get(pk=account_id)
                enqueue_command(account, MinerCommand.Action.STOP)
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                second_finished.set()
                close_old_connections()

        first = Thread(target=queue_start_while_holding_transaction, daemon=True)
        second = Thread(target=queue_later_stop, daemon=True)
        first.start()
        if not first_has_written.wait(timeout=5):
            raise AssertionError("first request did not reach its held transaction")
        second.start()
        if not second_started.wait(timeout=5):
            raise AssertionError("second request did not start")
        serialized = not second_finished.wait(timeout=0.1)
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        if first.is_alive() or second.is_alive():
            raise AssertionError("concurrent command threads did not finish")
        if not serialized:
            raise AssertionError(f"later request did not serialize: {errors}")
        if errors:
            raise AssertionError(f"concurrent command failed: {errors}")

        state = MinerInstanceState.objects.get(account_id=account_id)
        commands = list(
            MinerCommand.objects.filter(account_id=account_id).values_list("action", "status")
        )
        expected = [
            (MinerCommand.Action.START, MinerCommand.Status.CANCELLED),
            (MinerCommand.Action.STOP, MinerCommand.Status.QUEUED),
        ]
        if state.desired_state != MinerInstanceState.DesiredState.STOPPED:
            raise AssertionError(f"latest desired state was {state.desired_state!r}")
        if commands != expected:
            raise AssertionError(f"unexpected command history: {commands!r}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_launch_snapshot_is_exact_immutable_and_secret_free(tmp_path):
    config_path = make_config(tmp_path)
    sync_config_accounts(config_path)
    account = MinerAccount.objects.get(config_key="primary")
    preset = save_preset(name="Ordered", channels=["Three", "two", "one"])
    set_account_channel_selection(
        account,
        AccountChannelSelection.Mode.PRESET,
        preset=preset,
        enqueue_restart=False,
    )

    run = create_launch_snapshot(account, "worker-a", config=config_path)
    assert run.channels == ["Three", "two", "one"]
    assert run.source_name == "Ordered"
    assert run.worker_id == "worker-a"
    persisted_values = json.dumps(
        list(MinerRun.objects.values()),
        default=str,
        sort_keys=True,
    )
    assert "super-secret" not in persisted_values

    run.channels = ["different"]
    with pytest.raises(ValidationError, match="immutable"):
        run.save()


@pytest.mark.django_db
def test_import_missing_presets_file_imports_state_with_empty_preset_list(
    tmp_path,
    settings,
):
    config_path = make_config(tmp_path, autostart=True)
    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    (data_dir / "state.json").write_text(
        json.dumps(
            {
                "states": [
                    {
                        "user_id": "primary",
                        "assigned_preset": "__custom__",
                        "custom_channels": ["second", "first"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings.TWITCH_FARM_COOKIES_DIR = tmp_path / "cookies"
    output = io.StringIO()

    call_command(
        "import_legacy_data",
        config=str(config_path),
        data_dir=str(data_dir),
        stdout=output,
        stderr=io.StringIO(),
    )

    account = MinerAccount.objects.get(config_key="primary")
    assert "Imported 0 preset(s), 1 account state(s)" in output.getvalue()
    assert ChannelPreset.objects.count() == 0
    assert account.selection.mode == AccountChannelSelection.Mode.CUSTOM
    assert list(account.custom_channels.values_list("name", flat=True)) == ["second", "first"]
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.RUNNING
    import_log = ActionLog.objects.get(action="legacy_import")
    assert import_log.details["preset_count"] == 0
    assert import_log.details["state_count"] == 1


@pytest.mark.django_db
def test_import_missing_presets_file_still_requires_state_file(tmp_path):
    config_path = make_config(tmp_path)
    data_dir = tmp_path / "legacy"
    data_dir.mkdir()

    with pytest.raises(CommandError, match="Missing legacy state file"):
        call_command(
            "import_legacy_data",
            config=str(config_path),
            data_dir=str(data_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_import_dry_run_idempotence_orphans_and_replace(tmp_path, settings):
    config_path = make_config(tmp_path, autostart=True)
    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    presets_path = data_dir / "presets.json"
    presets_path.write_text(
        json.dumps({"presets": [{"name": "Games", "channels": ["one", "two"]}]}),
        encoding="utf-8",
    )
    (data_dir / "state.json").write_text(
        json.dumps(
            {
                "states": [
                    {
                        "user_id": "primary",
                        "is_running": False,
                        "pid": 12345,
                        "assigned_preset": "Games",
                        "custom_channels": ["saved_for_later"],
                    },
                    {
                        "user_id": "orphan",
                        "is_running": True,
                        "pid": 99999,
                        "assigned_preset": "__custom__",
                        "custom_channels": ["orphan_channel"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    settings.TWITCH_FARM_COOKIES_DIR = tmp_path / "cookies"

    call_command(
        "import_legacy_data",
        config=str(config_path),
        data_dir=str(data_dir),
        dry_run=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert MinerAccount.objects.count() == 0

    output = io.StringIO()
    call_command(
        "import_legacy_data",
        config=str(config_path),
        data_dir=str(data_dir),
        stdout=output,
        stderr=io.StringIO(),
    )
    assert "Imported 1 preset(s), 2 account state(s)" in output.getvalue()
    primary = MinerAccount.objects.get(config_key="primary")
    orphan = MinerAccount.objects.get(config_key="orphan")
    assert primary.runtime_state.desired_state == MinerInstanceState.DesiredState.RUNNING
    assert primary.runtime_state.advisory_pid is None
    assert primary.selection.preset.name == "Games"
    assert list(primary.custom_channels.values_list("name", flat=True)) == ["saved_for_later"]
    assert orphan.is_configured is False
    assert orphan.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED
    assert orphan.runtime_state.advisory_pid is None

    call_command(
        "import_legacy_data",
        config=str(config_path),
        data_dir=str(data_dir),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert ActionLog.objects.filter(action="legacy_import").count() == 1
    assert ChannelPreset.objects.count() == 1

    presets_path.write_text(
        json.dumps({"presets": [{"name": "Games", "channels": ["replacement"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(CommandError, match="--replace"):
        call_command(
            "import_legacy_data",
            config=str(config_path),
            data_dir=str(data_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    call_command(
        "import_legacy_data",
        config=str(config_path),
        data_dir=str(data_dir),
        replace=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert ChannelPreset.objects.get(name="Games").channel_names == ["replacement"]
