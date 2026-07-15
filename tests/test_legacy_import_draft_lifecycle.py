from __future__ import annotations

from datetime import timedelta
import io
import json
import zipfile

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import controller.legacy_import as legacy_import
from controller.legacy_import import (
    LegacyImportConflict,
    LegacyImportError,
    LegacyImportStale,
    confirm_legacy_import,
    prepare_legacy_import,
)
from controller.models import (
    ActionLog,
    ChannelPreset,
    FarmConfiguration,
    LegacyImportDraft,
    MinerAccount,
)


def _archive(*, include_preset: bool = False) -> bytes:
    config = """settings:
  autostart_instances: true
twitch_users:
  primary:
    username: PrimaryUser
    password: lifecycle-secret
default_channels:
  - alpha
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.yaml", config)
        archive.writestr("data/state.json", json.dumps({"states": []}))
        if include_preset:
            archive.writestr(
                "data/presets.json",
                json.dumps(
                    {"presets": [{"name": "Games", "channels": ["one"]}]}
                ),
            )
    return output.getvalue()


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user(
        username="draft-operator",
        password="test-password",
        is_staff=True,
    )


@pytest.mark.django_db
def test_expired_draft_cannot_apply_and_remains_unconsumed(staff):
    draft = prepare_legacy_import(_archive(include_preset=True), staff)
    LegacyImportDraft.objects.filter(pk=draft.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    with pytest.raises(LegacyImportError, match="expired"):
        confirm_legacy_import(draft.pk, staff)

    assert not MinerAccount.objects.exists()
    assert not ChannelPreset.objects.exists()
    assert not ActionLog.objects.exists()
    draft.refresh_from_db()
    assert draft.consumed_at is None


@pytest.mark.django_db
def test_successful_draft_is_single_use(staff):
    draft = prepare_legacy_import(_archive(), staff)

    first_result = confirm_legacy_import(draft.pk, staff)
    counts_after_first_apply = {
        "accounts": MinerAccount.objects.count(),
        "actions": ActionLog.objects.count(),
    }

    assert first_result.created_accounts == ("primary",)
    with pytest.raises(LegacyImportError, match="already been used"):
        confirm_legacy_import(draft.pk, staff)

    assert MinerAccount.objects.count() == counts_after_first_apply["accounts"]
    assert ActionLog.objects.count() == counts_after_first_apply["actions"]
    draft.refresh_from_db()
    assert draft.consumed_at is not None


@pytest.mark.django_db
def test_prepare_purges_expired_drafts_without_removing_live_drafts(staff):
    expired = prepare_legacy_import(_archive(), staff)
    live = prepare_legacy_import(_archive(), staff)
    LegacyImportDraft.objects.filter(pk=expired.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    newest = prepare_legacy_import(_archive(), staff)

    remaining_ids = set(LegacyImportDraft.objects.values_list("pk", flat=True))
    assert expired.pk not in remaining_ids
    assert live.pk in remaining_ids
    assert newest.pk in remaining_ids


@pytest.mark.django_db
def test_stale_confirmation_applies_nothing_and_keeps_operator_change(staff):
    draft = prepare_legacy_import(_archive(include_preset=True), staff)
    configuration = FarmConfiguration.load()
    configuration.default_channels = ["operator_change"]
    configuration.save()

    with pytest.raises(LegacyImportStale, match="database changed"):
        confirm_legacy_import(draft.pk, staff)

    assert FarmConfiguration.load().default_channels == ["operator_change"]
    assert not MinerAccount.objects.exists()
    assert not ChannelPreset.objects.exists()
    assert not ActionLog.objects.exists()
    draft.refresh_from_db()
    assert draft.consumed_at is None


@pytest.mark.django_db
def test_apply_failure_rolls_back_partial_writes_and_does_not_consume_draft(
    staff, monkeypatch
):
    draft = prepare_legacy_import(_archive(), staff)

    def fail_after_write(payload, plan, *, actor):
        MinerAccount.objects.create(
            config_key="transient",
            display_username="Must Roll Back",
            is_active=False,
        )
        raise IntegrityError("forced apply failure")

    monkeypatch.setattr(legacy_import, "_apply_diff_plan", fail_after_write)

    with pytest.raises(LegacyImportConflict, match="no changes were committed"):
        confirm_legacy_import(draft.pk, staff)

    assert not MinerAccount.objects.exists()
    assert not ActionLog.objects.exists()
    draft.refresh_from_db()
    assert draft.consumed_at is None


@pytest.mark.django_db
def test_confirm_is_actor_bound_and_wrong_actor_applies_nothing(staff):
    other_staff = get_user_model().objects.create_user(
        username="other-draft-operator",
        password="test-password",
        is_staff=True,
    )
    draft = prepare_legacy_import(_archive(), staff)

    with pytest.raises(LegacyImportError, match="belongs to another staff account"):
        confirm_legacy_import(draft.pk, other_staff)

    assert not MinerAccount.objects.exists()
    assert not ActionLog.objects.exists()
    draft.refresh_from_db()
    assert draft.consumed_at is None


@pytest.mark.django_db
def test_unknown_draft_ids_are_safe_in_confirm_and_cancel_api(staff):
    draft = prepare_legacy_import(_archive(), staff)
    client = Client()
    client.force_login(staff)

    unknown = "00000000-0000-0000-0000-000000000001"
    confirm_response = client.post(
        reverse("controller:api:settings_import_confirm", args=[unknown]),
        data="{}",
        content_type="application/json",
    )
    cancel_response = client.delete(
        reverse("controller:api:settings_import_delete", args=[unknown]),
        data="{}",
        content_type="application/json",
    )

    assert confirm_response.status_code == 400
    assert cancel_response.status_code == 404
    assert LegacyImportDraft.objects.filter(pk=draft.pk).exists()
    assert not MinerAccount.objects.exists()


@pytest.mark.django_db
def test_cancel_api_cannot_delete_another_actors_draft(staff):
    other_staff = get_user_model().objects.create_user(
        username="cancel-operator",
        password="test-password",
        is_staff=True,
    )
    draft = prepare_legacy_import(_archive(), staff)
    client = Client()
    client.force_login(other_staff)

    response = client.delete(
        reverse("controller:api:settings_import_delete", args=[draft.pk]),
        data="{}",
        content_type="application/json",
    )

    assert response.status_code == 404
    assert LegacyImportDraft.objects.filter(pk=draft.pk).exists()
