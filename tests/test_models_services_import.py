from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from django.core.exceptions import ValidationError

from controller.apps import configure_sqlite_connection
from controller.models import (
    AccountChannelSelection,
    AccountCredential,
    ActionLog,
    FarmConfiguration,
    MinerAccount,
    MinerCommand,
    MinerInstanceState,
    MinerRun,
)
from controller.services import (
    archive_account,
    create_account,
    create_launch_snapshot,
    enqueue_command,
    get_account_password,
    normalize_channels,
    reactivate_account,
    resolve_channels,
    save_preset,
    set_account_channel_selection,
    update_account,
    update_farm_configuration,
)


@pytest.fixture
def farm_configuration(db) -> FarmConfiguration:
    return update_farm_configuration(
        default_channels=["Alpha", "beta", "ALPHA"],
        autostart_new_accounts=False,
    )


@pytest.fixture
def db_account(farm_configuration: FarmConfiguration) -> MinerAccount:
    return create_account(
        config_key="primary",
        username="PrimaryUser",
        password="super-secret",
    )


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
def test_create_account_encrypts_credentials_and_sets_durable_start_intent():
    FarmConfiguration.objects.update_or_create(
        pk=1,
        defaults={
            "default_channels": ["Alpha", "beta"],
            "autostart_new_accounts": False,
        },
    )
    password = "never-store-this-in-plaintext"

    account = create_account(
        config_key="primary",
        username="PrimaryUser",
        password=password,
        start_after_save=True,
    )

    account.refresh_from_db()
    credential = AccountCredential.objects.get(account=account)
    state = MinerInstanceState.objects.get(account=account)
    assert account.config_key == "primary"
    assert account.display_username == "PrimaryUser"
    assert account.is_active is True
    assert account.selection.mode == AccountChannelSelection.Mode.DEFAULT
    assert credential.password_ciphertext != password
    assert password not in credential.password_ciphertext
    assert get_account_password(account) == password
    assert state.desired_state == MinerInstanceState.DesiredState.RUNNING
    assert MinerCommand.objects.get(account=account).action == MinerCommand.Action.START

    persisted_values = json.dumps(
        {
            "accounts": list(MinerAccount.objects.values()),
            "credentials": list(AccountCredential.objects.values()),
            "actions": list(ActionLog.objects.values()),
        },
        default=str,
        sort_keys=True,
    )
    assert password not in persisted_values
    assert "password" not in {field.name for field in MinerAccount._meta.get_fields()}


@pytest.mark.django_db
def test_update_account_preserves_key_reencrypts_password_and_restarts_running_account(
    db_account: MinerAccount,
):
    account = db_account
    state = account.runtime_state
    state.desired_state = MinerInstanceState.DesiredState.RUNNING
    state.save(update_fields=("desired_state", "updated_at"))
    old_ciphertext = account.credential.password_ciphertext
    replacement = "replacement-secret"

    updated = update_account(
        account,
        username="RenamedUser",
        password=replacement,
    )

    updated.refresh_from_db()
    updated.credential.refresh_from_db()
    assert updated.config_key == "primary"
    assert updated.display_username == "RenamedUser"
    assert updated.credential.password_ciphertext != old_ciphertext
    assert replacement not in updated.credential.password_ciphertext
    assert get_account_password(updated) == replacement
    assert MinerCommand.objects.get(account=updated).action == MinerCommand.Action.RESTART
    update_log = ActionLog.objects.get(account=updated, action="account_updated")
    assert update_log.details == {
        "username_changed": True,
        "credential_changed": True,
    }
    assert replacement not in json.dumps(update_log.details, sort_keys=True)

    updated.config_key = "renamed-key"
    with pytest.raises(ValidationError, match="internal key is immutable"):
        updated.save()
    updated.refresh_from_db()
    assert updated.config_key == "primary"


@pytest.mark.django_db
def test_archive_and_reactivate_preserve_history_and_require_credentials(
    db_account: MinerAccount,
):
    account = db_account
    run = create_launch_snapshot(account, "worker-before-archive")
    state = account.runtime_state
    state.desired_state = MinerInstanceState.DesiredState.RUNNING
    state.save(update_fields=("desired_state", "updated_at"))

    archived = archive_account(account)

    archived.refresh_from_db()
    archived.runtime_state.refresh_from_db()
    assert archived.is_active is False
    assert archived.configuration_fingerprint == ""
    assert archived.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED
    assert MinerCommand.objects.get(account=archived).action == MinerCommand.Action.STOP
    assert MinerRun.objects.filter(pk=run.pk, account=archived).exists()

    reactivated = reactivate_account(archived)
    reactivated.refresh_from_db()
    assert reactivated.is_active is True
    assert reactivated.configuration_fingerprint
    assert reactivated.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED

    archive_account(reactivated)
    AccountCredential.objects.filter(account=reactivated).delete()
    with pytest.raises(ValidationError, match="password"):
        reactivate_account(reactivated)
    reactivated.refresh_from_db()
    assert reactivated.is_active is False


@pytest.mark.django_db
def test_channel_resolution_selection_restart_and_command_coalescing(
    db_account: MinerAccount,
):
    account = db_account

    assert normalize_channels(" Alpha, beta\nALPHA ") == ["Alpha", "beta"]
    default = resolve_channels(account)
    assert default.channels == ("Alpha", "beta")
    assert default.mode == AccountChannelSelection.Mode.DEFAULT
    assert default.source_name == "farm defaults"

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


@pytest.mark.django_db
def test_database_default_changes_restart_only_running_default_accounts(
    farm_configuration: FarmConfiguration,
):
    default_account = create_account(
        config_key="default",
        username="DefaultUser",
        password="default-secret",
    )
    custom_account = create_account(
        config_key="custom",
        username="CustomUser",
        password="custom-secret",
        mode=AccountChannelSelection.Mode.CUSTOM,
        channels=["custom_channel"],
    )
    for account in (default_account, custom_account):
        account.runtime_state.desired_state = MinerInstanceState.DesiredState.RUNNING
        account.runtime_state.save(update_fields=("desired_state", "updated_at"))

    configuration = update_farm_configuration(
        default_channels=["NewOne", "new_two", "newone"],
        autostart_new_accounts=True,
    )

    assert configuration.pk == farm_configuration.pk == 1
    assert configuration.default_channels == ["NewOne", "new_two"]
    assert configuration.autostart_new_accounts is True
    default_account.refresh_from_db()
    custom_account.refresh_from_db()
    assert resolve_channels(default_account).channels == ("NewOne", "new_two")
    assert resolve_channels(custom_account).channels == ("custom_channel",)
    assert list(
        MinerCommand.objects.values_list("account__config_key", "action")
    ) == [("default", MinerCommand.Action.RESTART)]


@pytest.mark.django_db
def test_autostart_preference_change_does_not_restart_accounts(
    farm_configuration: FarmConfiguration,
):
    account = create_account(
        config_key="default",
        username="DefaultUser",
        password="default-secret",
    )
    account.runtime_state.desired_state = MinerInstanceState.DesiredState.RUNNING
    account.runtime_state.save(update_fields=("desired_state", "updated_at"))
    old_revision = account.channel_revision

    update_farm_configuration(
        default_channels=farm_configuration.default_channels,
        autostart_new_accounts=True,
    )

    account.refresh_from_db()
    assert account.channel_revision == old_revision
    assert not MinerCommand.objects.filter(account=account).exists()


def test_sqlite_immediate_transactions_preserve_latest_concurrent_command(
    tmp_path,
):
    database_path = tmp_path / "concurrent.sqlite3"
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_DEBUG": "1",
            "DJANGO_SECRET_KEY": "concurrency-test-only",
            "TWITCH_FARM_CREDENTIAL_KEYS": (
                "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
            ),
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

        from controller.models import MinerCommand, MinerInstanceState
        from controller.services import create_account, enqueue_command, update_farm_configuration

        update_farm_configuration(
            default_channels=["Alpha", "beta"],
            autostart_new_accounts=False,
        )
        account = create_account(
            config_key="primary",
            username="PrimaryUser",
            password="concurrency-secret",
        )
        account_id = account.pk
        first_has_written = Event()
        release_first = Event()
        second_started = Event()
        second_finished = Event()
        errors = []

        def queue_start_while_holding_transaction():
            close_old_connections()
            try:
                with transaction.atomic():
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
def test_launch_snapshot_is_exact_immutable_database_backed_and_secret_free(
    db_account: MinerAccount,
):
    account = db_account
    preset = save_preset(name="Ordered", channels=["Three", "two", "one"])
    set_account_channel_selection(
        account,
        AccountChannelSelection.Mode.PRESET,
        preset=preset,
        enqueue_restart=False,
    )

    run = create_launch_snapshot(account, "worker-a")
    assert run.channels == ["Three", "two", "one"]
    assert run.source_name == "Ordered"
    assert run.source_mode == AccountChannelSelection.Mode.PRESET
    assert run.worker_id == "worker-a"
    account.refresh_from_db()
    assert run.configuration_fingerprint == account.configuration_fingerprint
    persisted_values = json.dumps(
        list(MinerRun.objects.values()),
        default=str,
        sort_keys=True,
    )
    assert "super-secret" not in persisted_values

    save_preset(name="Ordered", channels=["replacement"], preset=preset)
    run.refresh_from_db()
    assert run.channels == ["Three", "two", "one"]
    assert create_launch_snapshot(account, "worker-b").channels == ["replacement"]

    run.channels = ["different"]
    with pytest.raises(ValidationError, match="immutable"):
        run.save()
