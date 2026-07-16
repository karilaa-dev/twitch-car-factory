from __future__ import annotations

import io
import json
import os
import pickle
import zipfile

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from controller.crypto import decrypt_json, decrypt_text, encrypt_text
from controller.legacy_import import (
    LegacyArchiveError,
    LegacyImportConflict,
    LegacyImportError,
    LegacyImportReplacementRequired,
    LegacyImportStale,
    confirm_legacy_import,
    parse_legacy_archive,
    prepare_legacy_import,
)
from controller.models import (
    AccountCredential,
    AccountSessionSeed,
    ActionLog,
    ChannelPreset,
    FarmConfiguration,
    LegacyImportDraft,
    MinerAccount,
    MinerInstanceState,
)
from controller.services import update_account


def _config(
    *,
    username: str = "PrimaryUser",
    password: str = "super-secret",
    defaults=("alpha",),
) -> str:
    channels = "\n".join(f"  - {channel}" for channel in defaults)
    return f"""settings:
  autostart_instances: true
twitch_users:
  primary:
    username: {username}
    password: {password}
default_channels:
{channels}
"""


def _archive(
    *,
    config: str | None = None,
    states: list[dict] | None = None,
    presets: list[dict] | None = None,
    cookies: dict[str, bytes] | None = None,
    wrapper: str = "",
    extra: dict[str, bytes] | None = None,
) -> bytes:
    prefix = f"{wrapper.strip('/')}/" if wrapper else ""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + "config.yaml", config or _config())
        archive.writestr(
            prefix + "data/state.json",
            json.dumps({"states": states or []}),
        )
        if presets is not None:
            archive.writestr(
                prefix + "data/presets.json",
                json.dumps({"presets": presets}),
            )
        for username, value in (cookies or {}).items():
            archive.writestr(prefix + f"cookies/{username}.pkl", value)
        for path, value in (extra or {}).items():
            archive.writestr(prefix + path, value)
    return output.getvalue()


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user(
        username="operator",
        password="test-password",
        is_staff=True,
    )


def test_parser_accepts_one_wrapper_and_normalizes_safe_cookie():
    cookie = pickle.dumps(
        [
            {"name": "login", "value": "primaryuser"},
            {"name": "auth-token", "value": "token-value"},
        ],
        protocol=4,
    )
    payload = parse_legacy_archive(
        _archive(
            wrapper="old-install",
            states=[
                {
                    "user_id": "primary",
                    "assigned_preset": "__custom__",
                    "custom_channels": ["One", "one", "Two"],
                    "pid": 1234,
                    "is_running": True,
                }
            ],
            cookies={"PrimaryUser": cookie},
            extra={"notes.txt": b"ignored"},
        )
    )

    assert payload.accounts[0].password == "super-secret"
    assert payload.states[0].custom_channels == ("One", "Two")
    assert payload.cookies[0].normalized_values()[1]["name"] == "auth-token"
    assert payload.ignored_files == ("notes.txt",)
    assert any("pid and is_running" in warning for warning in payload.warnings)
    assert len(payload.source_digest) == 64


def test_wrapped_archive_ignores_bounded_root_metadata_siblings():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("old-install/config.yaml", _config())
        archive.writestr("old-install/data/state.json", '{"states": []}')
        archive.writestr(".DS_Store", b"metadata")
        archive.writestr("__MACOSX/old-install", b"metadata")

    payload = parse_legacy_archive(output.getvalue())

    assert payload.accounts[0].config_key == "primary"
    assert payload.ignored_files == (".DS_Store", "__MACOSX/old-install")


def test_archive_rejects_traversal_and_duplicate_normalized_paths():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("config.yaml", _config())
        archive.writestr("data/state.json", '{"states": []}')
        archive.writestr("../escape", "bad")
    with pytest.raises(LegacyArchiveError, match="path-traversal"):
        parse_legacy_archive(output.getvalue())

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("config.yaml", _config())
        archive.writestr("CONFIG.YAML", _config())
        archive.writestr("data/state.json", '{"states": []}')
    with pytest.raises(LegacyArchiveError, match="duplicate normalized"):
        parse_legacy_archive(output.getvalue())


def test_malicious_or_cyclic_cookie_skips_new_account_without_execution(tmp_path):
    marker = tmp_path / "pickle-executed"

    class Exploit:
        def __reduce__(self):
            return os.system, (f"touch {marker}",)

    payload = parse_legacy_archive(
        _archive(cookies={"PrimaryUser": pickle.dumps(Exploit())})
    )
    assert payload.cookies == ()
    assert payload.cookie_issues[0].code == "invalid_cookie"
    assert not marker.exists()

    cyclic: list[object] = []
    cyclic.append(cyclic)
    payload = parse_legacy_archive(
        _archive(cookies={"PrimaryUser": pickle.dumps(cyclic)})
    )
    assert payload.cookies == ()
    assert payload.cookie_issues


@pytest.mark.django_db
def test_prepare_and_confirm_encrypt_secrets_and_never_start_accounts(staff):
    cookie_values = [
        {"name": "login", "value": "PrimaryUser"},
        {"name": "auth-token", "value": "private-token"},
    ]
    upload = _archive(
        states=[
            {
                "user_id": "primary",
                "assigned_preset": "Games",
                "custom_channels": ["saved"],
                "pid": 99,
                "is_running": True,
            },
            {
                "user_id": "orphan",
                "assigned_preset": "__custom__",
                "custom_channels": ["orphan_channel"],
            },
        ],
        presets=[{"name": "Games", "channels": ["one", "two"]}],
        cookies={"PrimaryUser": pickle.dumps(cookie_values)},
    )
    draft = prepare_legacy_import(upload, staff)

    assert draft.preview["accounts"]["create"][0]["username"] == "PrimaryUser"
    assert "super-secret" not in json.dumps(draft.preview)
    assert "private-token" not in json.dumps(draft.preview)
    assert "super-secret" not in draft.payload_ciphertext
    result = confirm_legacy_import(draft.pk, staff)

    primary = MinerAccount.objects.get(config_key="primary")
    orphan = MinerAccount.objects.get(config_key="orphan")
    assert result.created_accounts == ("primary", "orphan")
    assert primary.is_active is True
    assert orphan.is_active is False
    assert primary.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED
    assert primary.runtime_state.advisory_pid is None
    assert decrypt_text(primary.credential.password_ciphertext) == "super-secret"
    assert decrypt_json(primary.session_seed.payload_ciphertext) == cookie_values
    assert FarmConfiguration.load().default_channels == ["alpha"]
    assert FarmConfiguration.load().autostart_new_accounts is True
    audit = ActionLog.objects.get(action="legacy_import")
    encoded_audit = json.dumps(audit.details)
    assert "super-secret" not in encoded_audit
    assert "private-token" not in encoded_audit
    draft.refresh_from_db()
    assert draft.consumed_at is not None
    assert decrypt_json(draft.payload_ciphertext) == {"consumed": True}


@pytest.mark.django_db
def test_invalid_cookie_skips_only_new_account_and_imports_shared_data(staff):
    draft = prepare_legacy_import(
        _archive(
            presets=[{"name": "Games", "channels": ["one"]}],
            cookies={"PrimaryUser": b"not-a-pickle"},
        ),
        staff,
    )
    assert draft.preview["accounts"]["skipped"][0]["config_key"] == "primary"
    assert draft.preview["presets"]["create"] == [{"name": "Games"}]

    result = confirm_legacy_import(draft.pk, staff)
    assert result.skipped_accounts[0]["config_key"] == "primary"
    assert not MinerAccount.objects.exists()
    assert ChannelPreset.objects.filter(name="Games").exists()


@pytest.mark.django_db
def test_existing_account_cookie_is_rejected_without_replacing_session(staff):
    first = prepare_legacy_import(_archive(), staff)
    confirm_legacy_import(first.pk, staff)
    account = MinerAccount.objects.get(config_key="primary")
    assert not AccountSessionSeed.objects.filter(account=account).exists()

    cookie = pickle.dumps(
        [{"name": "auth-token", "value": "new-token"}], protocol=4
    )
    second = prepare_legacy_import(
        _archive(cookies={"PrimaryUser": cookie}), staff
    )
    assert second.preview["no_op"] is True
    assert second.preview["cookies"]["rejected"][0]["code"] == "cookie_for_existing_account"
    result = confirm_legacy_import(second.pk, staff)
    assert result.no_op is True
    assert not AccountSessionSeed.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_stale_preview_and_ui_owned_account_conflicts_are_fail_closed(staff):
    draft = prepare_legacy_import(_archive(), staff)
    configuration = FarmConfiguration.load()
    configuration.default_channels = ["changed"]
    configuration.save()
    with pytest.raises(LegacyImportStale):
        confirm_legacy_import(draft.pk, staff)
    assert not MinerAccount.objects.exists()

    account = MinerAccount.objects.create(
        config_key="primary",
        display_username="PrimaryUser",
        is_active=True,
    )
    AccountCredential.objects.create(
        account=account,
        password_ciphertext=encrypt_text("different-password"),
    )
    second = prepare_legacy_import(
        _archive(config=_config(defaults=("changed",))), staff
    )
    assert second.preview["can_apply"] is False
    assert second.preview["conflicts"][0]["code"] == "unowned_account"
    with pytest.raises(LegacyImportConflict):
        confirm_legacy_import(second.pk, staff)


@pytest.mark.django_db
def test_owned_replacement_requires_acknowledgement_and_typed_replace(staff):
    states = [
        {
            "user_id": "primary",
            "assigned_preset": "Games",
            "custom_channels": [],
        }
    ]
    first = prepare_legacy_import(
        _archive(
            states=states,
            presets=[{"name": "Games", "channels": ["one"]}],
        ),
        staff,
    )
    confirm_legacy_import(first.pk, staff)

    second = prepare_legacy_import(
        _archive(
            states=states,
            presets=[{"name": "Games", "channels": ["replacement"]}],
        ),
        staff,
    )
    assert second.preview["requires_replace"] is True
    assert second.preview["presets"]["update"] == [{"name": "Games"}]
    with pytest.raises(LegacyImportReplacementRequired):
        confirm_legacy_import(second.pk, staff)

    result = confirm_legacy_import(
        second.pk,
        staff,
        replace=True,
        acknowledged=True,
        confirmation="REPLACE",
    )
    assert result.updated_presets == ("Games",)
    assert ChannelPreset.objects.get(name="Games").channel_names == ["replacement"]


@pytest.mark.django_db
def test_imported_username_change_discards_seed_for_the_old_identity(staff):
    first = prepare_legacy_import(
        _archive(
            cookies={
                "PrimaryUser": pickle.dumps(
                    [{"name": "auth-token", "value": "old-session"}],
                    protocol=4,
                )
            }
        ),
        staff,
    )
    confirm_legacy_import(first.pk, staff)
    account = MinerAccount.objects.get(config_key="primary")
    assert AccountSessionSeed.objects.filter(account=account).exists()

    second = prepare_legacy_import(
        _archive(config=_config(username="RenamedUser")),
        staff,
    )
    confirm_legacy_import(
        second.pk,
        staff,
        replace=True,
        acknowledged=True,
        confirmation="REPLACE",
    )

    account.refresh_from_db()
    assert account.display_username == "RenamedUser"
    assert not AccountSessionSeed.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_prepend_key_rotation_keeps_a_live_draft_valid(staff):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(old_key,)):
        draft = prepare_legacy_import(_archive(), staff)

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(new_key, old_key)):
        result = confirm_legacy_import(draft.pk, staff)
        account = MinerAccount.objects.get(config_key="primary")
        stored_password = Fernet(new_key).decrypt(
            account.credential.password_ciphertext.encode("ascii")
        ).decode("utf-8")

    assert result.created_accounts == ("primary",)
    assert stored_password == "super-secret"


@pytest.mark.django_db
def test_draft_with_missing_decryption_key_fails_safely(staff):
    original_key = Fernet.generate_key().decode("ascii")
    wrong_key = Fernet.generate_key().decode("ascii")
    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(original_key,)):
        draft = prepare_legacy_import(_archive(), staff)

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(wrong_key,)):
        with pytest.raises(LegacyImportError, match="cannot be decrypted") as captured:
            confirm_legacy_import(draft.pk, staff)

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert "super-secret" not in rendered
    assert draft.payload_ciphertext not in rendered
    assert not MinerAccount.objects.exists()


@pytest.mark.django_db
def test_ui_touched_unchanged_account_does_not_regain_import_ownership(staff):
    first = prepare_legacy_import(_archive(), staff)
    confirm_legacy_import(first.pk, staff)
    account = MinerAccount.objects.get(config_key="primary")

    # The UI deliberately rewrites the same semantic credential.  Fernet makes
    # the ciphertext new, and the audit record permanently revokes importer
    # ownership even though the next ZIP happens to match again.
    update_account(
        account,
        username="PrimaryUser",
        password="super-secret",
        actor=staff,
    )
    second = prepare_legacy_import(
        _archive(presets=[{"name": "Added", "channels": ["one"]}]),
        staff,
    )
    assert second.preview["accounts"]["unchanged"][0]["config_key"] == "primary"
    confirm_legacy_import(
        second.pk,
        staff,
        replace=True,
        acknowledged=True,
        confirmation="REPLACE",
    )

    third = prepare_legacy_import(
        _archive(
            config=_config(password="different-password"),
            presets=[{"name": "Added", "channels": ["one"]}],
        ),
        staff,
    )
    assert third.preview["can_apply"] is False
    assert any(
        conflict["code"] == "unowned_account"
        for conflict in third.preview["conflicts"]
    )


@pytest.mark.django_db
def test_import_replacement_previews_and_stops_running_default_accounts(staff):
    first = prepare_legacy_import(_archive(), staff)
    confirm_legacy_import(first.pk, staff)
    account = MinerAccount.objects.get(config_key="primary")
    account.runtime_state.desired_state = MinerInstanceState.DesiredState.RUNNING
    account.runtime_state.save(update_fields=("desired_state", "updated_at"))

    second = prepare_legacy_import(
        _archive(config=_config(defaults=("replacement",))),
        staff,
    )
    assert second.preview["accounts"]["reset"] == [
        {
            "config_key": "primary",
            "key": "primary",
            "username": "PrimaryUser",
            "has_cookie": False,
            "message": "Running intent will be reset to stopped before imported farm defaults are applied.",
        }
    ]
    confirm_legacy_import(
        second.pk,
        staff,
        replace=True,
        acknowledged=True,
        confirmation="REPLACE",
    )

    account.runtime_state.refresh_from_db()
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED


@pytest.mark.django_db
def test_import_replacement_stops_accounts_affected_by_preset_changes(staff):
    states = [
        {
            "user_id": "primary",
            "assigned_preset": "Games",
            "custom_channels": [],
        }
    ]
    first = prepare_legacy_import(
        _archive(
            states=states,
            presets=[{"name": "Games", "channels": ["one"]}],
        ),
        staff,
    )
    confirm_legacy_import(first.pk, staff)
    account = MinerAccount.objects.get(config_key="primary")
    account.runtime_state.desired_state = MinerInstanceState.DesiredState.RUNNING
    account.runtime_state.save(update_fields=("desired_state", "updated_at"))

    second = prepare_legacy_import(
        _archive(
            states=states,
            presets=[{"name": "Games", "channels": ["two"]}],
        ),
        staff,
    )
    assert "preset Games" in second.preview["accounts"]["reset"][0]["message"]
    confirm_legacy_import(
        second.pk,
        staff,
        replace=True,
        acknowledged=True,
        confirmation="REPLACE",
    )

    account.runtime_state.refresh_from_db()
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED


@pytest.mark.django_db
def test_settings_import_api_previews_and_confirms_without_rendering_secrets(staff):
    password = "view-only-password-secret"
    auth_token = "view-only-auth-token-secret"
    archive = _archive(
        config=_config(password=password),
        cookies={
            "PrimaryUser": pickle.dumps(
                [
                    {"name": "login", "value": "PrimaryUser"},
                    {"name": "auth-token", "value": auth_token},
                ],
                protocol=4,
            )
        },
    )
    client = Client()
    client.force_login(staff)

    preview_response = client.post(
        reverse("controller:api:settings_imports"),
        {
            "archive": SimpleUploadedFile(
                "legacy-install.zip",
                archive,
                content_type="application/zip",
            )
        },
    )

    assert preview_response.status_code == 201
    preview_json = preview_response.content.decode()
    assert "PrimaryUser" in preview_json
    assert password not in preview_json
    assert auth_token not in preview_json
    assert not MinerAccount.objects.exists()
    draft = LegacyImportDraft.objects.get()
    assert draft.actor == staff
    assert draft.consumed_at is None

    confirm_response = client.post(
        reverse("controller:api:settings_import_confirm", args=[draft.pk]),
        data=json.dumps({"replace": False, "acknowledged": False, "confirmation": ""}),
        content_type="application/json",
    )

    assert confirm_response.status_code == 200
    result_json = confirm_response.content.decode()
    assert '"created_accounts":["primary"]' in result_json
    assert password not in result_json
    assert auth_token not in result_json
    account = MinerAccount.objects.get(config_key="primary")
    assert account.runtime_state.desired_state == MinerInstanceState.DesiredState.STOPPED
    draft.refresh_from_db()
    assert draft.consumed_at is not None


@pytest.mark.django_db
def test_settings_import_preview_and_confirm_require_csrf(staff):
    client = Client(enforce_csrf_checks=True)
    client.force_login(staff)

    preview_response = client.post(
        reverse("controller:api:settings_imports"),
        {
            "archive": SimpleUploadedFile(
                "legacy-install.zip",
                _archive(),
                content_type="application/zip",
            )
        },
    )

    assert preview_response.status_code == 403
    assert not LegacyImportDraft.objects.exists()

    draft = prepare_legacy_import(_archive(), staff)
    confirm_response = client.post(
        reverse("controller:api:settings_import_confirm", args=[draft.pk]),
        data=json.dumps({}),
        content_type="application/json",
    )

    assert confirm_response.status_code == 403
    assert not MinerAccount.objects.exists()
    draft.refresh_from_db()
    assert draft.consumed_at is None


@pytest.mark.django_db
def test_settings_import_confirm_rejects_a_draft_owned_by_another_staff_user(staff):
    other_staff = get_user_model().objects.create_user(
        username="other-operator",
        password="test-password",
        is_staff=True,
    )
    draft = prepare_legacy_import(_archive(), staff)
    client = Client()
    client.force_login(other_staff)

    response = client.post(
        reverse("controller:api:settings_import_confirm", args=[draft.pk]),
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert "belongs to another staff account" in response.content.decode()
    assert not MinerAccount.objects.exists()
    draft.refresh_from_db()
    assert draft.consumed_at is None
