"""Transactional controller operations shared by the web app and supervisor."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable, Sequence

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .crypto import decrypt_text, encrypt_text
from .models import (
    AccountCredential,
    AccountChannelSelection,
    AccountCustomChannel,
    AccountSessionSeed,
    ActionLog,
    ChannelPreset,
    FarmConfiguration,
    LegacyImportDraft,
    MinerAccount,
    MinerCommand,
    MinerInstanceState,
    MinerRun,
    PresetChannel,
)


CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,100}$")


def purge_expired_legacy_import_drafts(*, at=None) -> int:
    """Delete expired encrypted drafts; safe for web and worker maintenance."""

    deleted, _ = LegacyImportDraft.objects.filter(
        expires_at__lte=at or timezone.now()
    ).delete()
    return deleted


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    mode: str
    source_name: str
    channels: tuple[str, ...]
    fingerprint: str


def normalize_channels(
    value: str | Iterable[str],
    *,
    require_nonempty: bool = True,
) -> list[str]:
    """Normalize comma/newline input and deduplicate without reordering it."""

    if isinstance(value, str):
        raw_values: Sequence[str] = (value,)
    else:
        try:
            raw_values = tuple(value)
        except TypeError as exc:
            raise ValidationError("Channels must be text or an iterable of channel names.") from exc

    channels: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise ValidationError("Every channel must be a string.")
        for raw_channel in re.split(r"[,\r\n]+", raw_value):
            channel = raw_channel.strip()
            if not channel:
                continue
            if not CHANNEL_RE.fullmatch(channel):
                raise ValidationError(f"Invalid Twitch channel name: {channel!r}.")
            folded = channel.casefold()
            if folded not in seen:
                channels.append(channel)
                seen.add(folded)

    if require_nonempty and not channels:
        raise ValidationError("At least one channel is required.")
    return channels


def compute_configuration_fingerprint(
    *,
    account_key: str,
    username: str,
    mode: str,
    source_name: str,
    channels: Iterable[str],
) -> str:
    """Hash a launch specification without including its password."""

    payload = {
        "account_key": account_key,
        "username": username.casefold(),
        "mode": mode,
        "source_name": source_name.casefold(),
        "channels": [channel.casefold() for channel in channels],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_channels(account: MinerAccount) -> ChannelResolution:
    """Resolve and validate the exact channels that a fresh launch must use."""

    if not account.is_active:
        raise ValidationError(f"Account {account.config_key!r} is archived.")
    if not AccountCredential.objects.filter(account=account).exists():
        raise ValidationError(f"Account {account.config_key!r} has no stored credentials.")

    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        selection = AccountChannelSelection(account=account, mode=AccountChannelSelection.Mode.DEFAULT)

    if selection.mode == AccountChannelSelection.Mode.DEFAULT:
        channels = normalize_channels(FarmConfiguration.load().default_channels)
        source_name = "farm defaults"
    elif selection.mode == AccountChannelSelection.Mode.CUSTOM:
        channels = normalize_channels(
            account.custom_channels.order_by("position", "id").values_list("name", flat=True)
        )
        source_name = account.config_key
    elif selection.mode == AccountChannelSelection.Mode.PRESET:
        if not selection.preset_id:
            raise ValidationError("Preset mode requires a preset.")
        channels = normalize_channels(
            selection.preset.channels.order_by("position", "id").values_list("name", flat=True)
        )
        source_name = selection.preset.name
    else:
        raise ValidationError(f"Unknown channel selection mode: {selection.mode!r}.")

    fingerprint = compute_configuration_fingerprint(
        account_key=account.config_key,
        username=account.display_username,
        mode=selection.mode,
        source_name=source_name,
        channels=channels,
    )
    return ChannelResolution(
        mode=selection.mode,
        source_name=source_name,
        channels=tuple(channels),
        fingerprint=fingerprint,
    )


resolve_effective_channels = resolve_channels


def _best_effort_fingerprint(account: MinerAccount) -> str:
    """Return a fingerprint when possible without making an invalid account runnable."""

    selection = account.selection
    if selection.mode == AccountChannelSelection.Mode.CUSTOM:
        channels = normalize_channels(
            account.custom_channels.order_by("position", "id").values_list("name", flat=True)
        )
        source_name = account.config_key
    elif selection.mode == AccountChannelSelection.Mode.PRESET and selection.preset_id:
        channels = normalize_channels(
            selection.preset.channels.order_by("position", "id").values_list("name", flat=True)
        )
        source_name = selection.preset.name
    else:
        return ""
    return compute_configuration_fingerprint(
        account_key=account.config_key,
        username=account.display_username,
        mode=selection.mode,
        source_name=source_name,
        channels=channels,
    )


def _refresh_account_fingerprint(account: MinerAccount) -> None:
    try:
        fingerprint = resolve_channels(account).fingerprint
    except ValidationError:
        try:
            fingerprint = _best_effort_fingerprint(account)
        except (AccountChannelSelection.DoesNotExist, ValidationError):
            fingerprint = ""
    account.configuration_fingerprint = fingerprint
    account.save(update_fields=("configuration_fingerprint", "updated_at"))


@sensitive_variables()
def get_account_password(account: MinerAccount | int) -> str:
    account_id = account if isinstance(account, int) else account.pk
    try:
        credential = AccountCredential.objects.get(account_id=account_id)
    except AccountCredential.DoesNotExist as exc:
        raise ValidationError("This account has no stored Twitch password.") from exc
    return decrypt_text(credential.password_ciphertext)


def _normalize_account_identity(config_key: str, username: str) -> tuple[str, str]:
    key = config_key.strip()
    clean_username = username.strip()
    if not key or len(key) > 150:
        raise ValidationError("Account key must be between 1 and 150 characters.")
    if not CHANNEL_RE.fullmatch(clean_username):
        raise ValidationError("Enter a valid Twitch username.")
    return key, clean_username


@sensitive_variables()
def _validate_password(password: str) -> str:
    if not isinstance(password, str) or not password.strip():
        raise ValidationError("Twitch password cannot be empty.")
    if len(password) > 4096:
        raise ValidationError("Twitch password is too long.")
    return password


@transaction.atomic
@sensitive_variables()
def create_account(
    *,
    config_key: str,
    username: str,
    password: str,
    mode: str = AccountChannelSelection.Mode.DEFAULT,
    channels: str | Iterable[str] | None = None,
    preset: ChannelPreset | int | str | None = None,
    start_after_save: bool = False,
    actor=None,
) -> MinerAccount:
    key, clean_username = _normalize_account_identity(config_key, username)
    secret = _validate_password(password)
    if MinerAccount.objects.filter(config_key__iexact=key).exists():
        raise ValidationError("An account with this key already exists.")
    if MinerAccount.objects.filter(display_username__iexact=clean_username).exists():
        raise ValidationError("This Twitch username is already managed.")

    account = MinerAccount.objects.create(
        config_key=key,
        display_username=clean_username,
        is_active=True,
    )
    AccountCredential.objects.create(
        account=account,
        password_ciphertext=encrypt_text(secret),
    )
    AccountChannelSelection.objects.create(account=account)
    MinerInstanceState.objects.create(
        account=account,
        desired_state=MinerInstanceState.DesiredState.STOPPED,
        observed_state=MinerInstanceState.ObservedState.UNKNOWN,
    )
    if mode != AccountChannelSelection.Mode.DEFAULT or channels is not None or preset is not None:
        set_account_channel_selection(
            account,
            mode,
            channels=channels,
            preset=preset,
            actor=actor,
            enqueue_restart=False,
        )
    else:
        _refresh_account_fingerprint(account)

    ActionLog.objects.create(
        actor=actor,
        account=account,
        action="account_created",
        message=f"Created account {account.config_key}.",
        details={"username": account.display_username},
    )
    if start_after_save:
        # Validate the complete launch source before durable running intent is set.
        resolve_channels(account)
        enqueue_command(
            account,
            MinerCommand.Action.START,
            actor=actor,
            reason="Start requested while creating the account.",
        )
    return account


@transaction.atomic
@sensitive_variables()
def update_account(
    account: MinerAccount,
    *,
    username: str,
    password: str | None = None,
    actor=None,
) -> MinerAccount:
    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    _, clean_username = _normalize_account_identity(account.config_key, username)
    duplicate = MinerAccount.objects.filter(display_username__iexact=clean_username).exclude(
        pk=account.pk
    )
    if duplicate.exists():
        raise ValidationError("This Twitch username is already managed.")

    username_changed = account.display_username != clean_username
    credential_changed = password is not None and password != ""
    if username_changed:
        account.display_username = clean_username
        account.save(update_fields=("display_username", "updated_at"))
        AccountSessionSeed.objects.filter(account=account).delete()
    if credential_changed:
        secret = _validate_password(password or "")
        AccountCredential.objects.update_or_create(
            account=account,
            defaults={"password_ciphertext": encrypt_text(secret)},
        )
    _refresh_account_fingerprint(account)

    ActionLog.objects.create(
        actor=actor,
        account=account,
        action="account_updated",
        message=f"Updated account {account.config_key}.",
        details={
            "username_changed": username_changed,
            "credential_changed": credential_changed,
        },
    )
    state, _ = MinerInstanceState.objects.get_or_create(account=account)
    if (
        account.is_active
        and (username_changed or credential_changed)
        and state.desired_state == MinerInstanceState.DesiredState.RUNNING
    ):
        enqueue_command(
            account,
            MinerCommand.Action.RESTART,
            actor=actor,
            reason="Account credentials changed.",
        )
    return account


@transaction.atomic
def archive_account(account: MinerAccount, *, actor=None) -> MinerAccount:
    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    state, _ = MinerInstanceState.objects.get_or_create(account=account)
    if state.desired_state == MinerInstanceState.DesiredState.RUNNING or state.current_run_id:
        enqueue_command(
            account,
            MinerCommand.Action.STOP,
            actor=actor,
            reason="Account archived from the web control room.",
        )
    account.is_active = False
    account.configuration_fingerprint = ""
    account.save(update_fields=("is_active", "configuration_fingerprint", "updated_at"))
    ActionLog.objects.create(
        actor=actor,
        account=account,
        action="account_archived",
        message=f"Archived account {account.config_key}.",
    )
    return account


@transaction.atomic
def reactivate_account(account: MinerAccount, *, actor=None) -> MinerAccount:
    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    if not AccountCredential.objects.filter(account=account).exists():
        raise ValidationError("Add a Twitch password before reactivating this account.")
    account.is_active = True
    account.save(update_fields=("is_active", "updated_at"))
    resolve_channels(account)
    _refresh_account_fingerprint(account)
    ActionLog.objects.create(
        actor=actor,
        account=account,
        action="account_reactivated",
        message=f"Reactivated account {account.config_key}.",
    )
    return account


@transaction.atomic
def update_farm_configuration(
    *,
    default_channels: str | Iterable[str],
    autostart_new_accounts: bool,
    actor=None,
) -> FarmConfiguration:
    channels = normalize_channels(default_channels)
    configuration = FarmConfiguration.objects.select_for_update().filter(pk=1).first()
    if configuration is None:
        configuration = FarmConfiguration(pk=1)
    channels_changed = list(configuration.default_channels) != channels
    preference_changed = (
        configuration.autostart_new_accounts != bool(autostart_new_accounts)
    )
    changed = channels_changed or preference_changed
    configuration.default_channels = channels
    configuration.autostart_new_accounts = bool(autostart_new_accounts)
    configuration.save()
    if changed:
        if channels_changed:
            accounts = list(
                MinerAccount.objects.select_for_update()
                .filter(is_active=True, selection__mode=AccountChannelSelection.Mode.DEFAULT)
                .order_by("pk")
            )
            for account in accounts:
                MinerAccount.objects.filter(pk=account.pk).update(
                    channel_revision=F("channel_revision") + 1
                )
                account.refresh_from_db(fields=("channel_revision", "updated_at"))
                _refresh_account_fingerprint(account)
                state, _ = MinerInstanceState.objects.get_or_create(account=account)
                if state.desired_state == MinerInstanceState.DesiredState.RUNNING:
                    enqueue_command(
                        account,
                        MinerCommand.Action.RESTART,
                        actor=actor,
                        reason="Farm default channels changed.",
                    )
        ActionLog.objects.create(
            actor=actor,
            action="farm_settings_updated",
            message="Updated farm defaults.",
            details={
                "default_channels": channels,
                "autostart_new_accounts": bool(autostart_new_accounts),
            },
        )
    return configuration


@transaction.atomic
def enqueue_command(
    account: MinerAccount,
    action: str,
    *,
    actor=None,
    reason: str = "",
) -> MinerCommand:
    """Queue one lifecycle command and make the latest desired state durable."""

    try:
        action = MinerCommand.Action(action)
    except ValueError as exc:
        raise ValidationError(f"Unknown miner command: {action!r}.") from exc

    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    if action != MinerCommand.Action.STOP:
        if not account.is_active:
            raise ValidationError("Archived accounts cannot be started or restarted.")
        if not AccountCredential.objects.filter(account=account).exists():
            raise ValidationError("Add a Twitch password before starting this account.")
        resolve_channels(account)
    state, _ = MinerInstanceState.objects.select_for_update().get_or_create(account=account)
    active = MinerCommand.objects.select_for_update().filter(
        account=account,
        status__in=(MinerCommand.Status.QUEUED, MinerCommand.Status.LEASED),
    )
    pending = active.filter(status=MinerCommand.Status.QUEUED)

    desired = (
        MinerInstanceState.DesiredState.STOPPED
        if action == MinerCommand.Action.STOP
        else MinerInstanceState.DesiredState.RUNNING
    )
    state.desired_state = desired
    # Recovery retry state is taken over only after the worker validates the
    # replacement launch snapshot.
    state.save(update_fields=("desired_state", "updated_at"))

    if action == MinerCommand.Action.STOP:
        pending.filter(action__in=(MinerCommand.Action.START, MinerCommand.Action.RESTART)).update(
            status=MinerCommand.Status.CANCELLED,
            completed_at=timezone.now(),
            error="Superseded by a stop command.",
        )
    else:
        pending.filter(action=MinerCommand.Action.STOP).update(
            status=MinerCommand.Status.CANCELLED,
            completed_at=timezone.now(),
            error=f"Superseded by a {action} command.",
        )
        if action == MinerCommand.Action.RESTART:
            pending.filter(action=MinerCommand.Action.START).update(
                status=MinerCommand.Status.CANCELLED,
                completed_at=timezone.now(),
                error="Superseded by a restart command.",
            )

    # A repeated action coalesces even while the singleton worker owns the
    # first command. Creating a second RESTART during startup grace would stop
    # the just-launched replacement and cause a needless second outage.
    command = active.filter(action=action).order_by("created_at", "id").first()
    if command is None:
        command = MinerCommand.objects.create(
            account=account,
            action=action,
            actor=actor,
            reason=reason[:255],
        )

    ActionLog.objects.create(
        actor=actor,
        account=account,
        action=f"miner_{action}",
        message=f"Requested {action} for {account.config_key}.",
        details={"command_id": command.pk, "reason": reason[:255]},
    )
    return command


@transaction.atomic
def enqueue_all(
    action: str,
    *,
    actor=None,
    reason: str = "",
) -> list[MinerCommand]:
    commands = []
    for account in MinerAccount.objects.filter(
        is_active=True,
        credential__isnull=False,
    ).order_by("config_key"):
        commands.append(enqueue_command(account, action, actor=actor, reason=reason))
    return commands


@transaction.atomic
def set_account_channel_selection(
    account: MinerAccount,
    mode: str,
    *,
    channels: str | Iterable[str] | None = None,
    preset: ChannelPreset | int | str | None = None,
    actor=None,
    enqueue_restart: bool = True,
) -> AccountChannelSelection:
    """Atomically update a channel source and restart a desired-running miner."""

    try:
        mode = AccountChannelSelection.Mode(mode)
    except ValueError as exc:
        raise ValidationError(f"Unknown channel selection mode: {mode!r}.") from exc

    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    selection, _ = AccountChannelSelection.objects.select_for_update().get_or_create(account=account)

    selected_preset: ChannelPreset | None = None
    if mode == AccountChannelSelection.Mode.PRESET:
        if isinstance(preset, ChannelPreset):
            selected_preset = ChannelPreset.objects.get(pk=preset.pk)
        elif isinstance(preset, int):
            selected_preset = ChannelPreset.objects.get(pk=preset)
        elif isinstance(preset, str):
            selected_preset = ChannelPreset.objects.get(name=preset)
        else:
            raise ValidationError("Preset mode requires a preset.")
        normalize_channels(selected_preset.channels.values_list("name", flat=True))
    elif preset is not None:
        raise ValidationError("A preset can only be supplied in preset mode.")

    if mode == AccountChannelSelection.Mode.CUSTOM:
        if channels is None:
            raise ValidationError("Custom mode requires channels.")
        normalized = normalize_channels(channels)
        account.custom_channels.all().delete()
        AccountCustomChannel.objects.bulk_create(
            AccountCustomChannel(account=account, name=name, position=position)
            for position, name in enumerate(normalized)
        )
    elif channels is not None:
        raise ValidationError("Custom channels can only be supplied in custom mode.")

    selection.mode = mode
    selection.preset = selected_preset
    selection.full_clean()
    selection.save()

    MinerAccount.objects.filter(pk=account.pk).update(channel_revision=F("channel_revision") + 1)
    account.refresh_from_db(fields=("channel_revision", "configuration_fingerprint", "updated_at"))
    _refresh_account_fingerprint(account)

    ActionLog.objects.create(
        actor=actor,
        account=account,
        action="channel_selection_changed",
        message=f"Changed {account.config_key} channel source to {mode}.",
        details={"mode": mode, "preset_id": selected_preset.pk if selected_preset else None},
    )

    state, _ = MinerInstanceState.objects.get_or_create(account=account)
    if enqueue_restart and state.desired_state == MinerInstanceState.DesiredState.RUNNING:
        enqueue_command(
            account,
            MinerCommand.Action.RESTART,
            actor=actor,
            reason="Channel selection changed.",
        )
    return selection


@transaction.atomic
def save_preset(
    *,
    name: str,
    channels: str | Iterable[str],
    preset: ChannelPreset | None = None,
    actor=None,
) -> ChannelPreset:
    """Create/update a preset and restart affected desired-running accounts."""

    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 150:
        raise ValidationError("Preset name must be between 1 and 150 characters.")
    normalized_channels = normalize_channels(channels)

    duplicate = ChannelPreset.objects.filter(name__iexact=normalized_name)
    if preset is not None:
        preset = ChannelPreset.objects.select_for_update().get(pk=preset.pk)
        duplicate = duplicate.exclude(pk=preset.pk)
    if duplicate.exists():
        raise ValidationError("A preset with this name already exists.")

    created = preset is None
    if created:
        preset = ChannelPreset.objects.create(name=normalized_name)
    else:
        preset.name = normalized_name
        preset.save(update_fields=("name", "updated_at"))
        preset.channels.all().delete()

    PresetChannel.objects.bulk_create(
        PresetChannel(preset=preset, name=channel, position=position)
        for position, channel in enumerate(normalized_channels)
    )

    affected = list(
        MinerAccount.objects.select_for_update().filter(selection__preset=preset).order_by("pk")
    )
    for account in affected:
        MinerAccount.objects.filter(pk=account.pk).update(channel_revision=F("channel_revision") + 1)
        account.refresh_from_db(fields=("channel_revision", "configuration_fingerprint", "updated_at"))
        _refresh_account_fingerprint(account)
        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        if state.desired_state == MinerInstanceState.DesiredState.RUNNING:
            enqueue_command(
                account,
                MinerCommand.Action.RESTART,
                actor=actor,
                reason=f"Preset {preset.name} changed.",
            )

    ActionLog.objects.create(
        actor=actor,
        action="preset_created" if created else "preset_updated",
        message=f"{'Created' if created else 'Updated'} preset {preset.name}.",
        details={"preset_id": preset.pk, "channels": normalized_channels},
    )
    return preset


@transaction.atomic
def delete_preset(preset: ChannelPreset, *, actor=None) -> None:
    preset = ChannelPreset.objects.select_for_update().get(pk=preset.pk)
    if preset.account_selections.exists():
        raise ValidationError("This preset is assigned to one or more accounts.")
    preset_id = preset.pk
    preset_name = preset.name
    preset.delete()
    ActionLog.objects.create(
        actor=actor,
        action="preset_deleted",
        message=f"Deleted preset {preset_name}.",
        details={"preset_id": preset_id},
    )


@transaction.atomic
def create_launch_snapshot(
    account: MinerAccount,
    worker_id: str = "",
) -> MinerRun:
    """Validate DB state and persist an immutable, secret-free launch spec."""

    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    resolution = resolve_channels(account)
    run = MinerRun(
        account=account,
        source_mode=resolution.mode,
        source_name=resolution.source_name,
        channels=list(resolution.channels),
        configuration_fingerprint=resolution.fingerprint,
        channel_revision=account.channel_revision,
        worker_id=worker_id,
    )
    run.full_clean()
    run.save()
    if account.configuration_fingerprint != resolution.fingerprint:
        account.configuration_fingerprint = resolution.fingerprint
        account.save(update_fields=("configuration_fingerprint", "updated_at"))
    return run


def record_action(
    action: str,
    *,
    actor=None,
    account: MinerAccount | None = None,
    message: str = "",
    details: dict | None = None,
) -> ActionLog:
    return ActionLog.objects.create(
        actor=actor,
        account=account,
        action=action,
        message=message[:255],
        details=details or {},
    )
