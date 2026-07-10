"""Transactional controller operations shared by the web app and supervisor."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Sequence

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .config import CHANNEL_RE, ConfigError, FarmConfig, load_config
from .models import (
    AccountChannelSelection,
    AccountCustomChannel,
    ActionLog,
    ChannelPreset,
    MinerAccount,
    MinerCommand,
    MinerInstanceState,
    MinerRun,
    PresetChannel,
)


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    mode: str
    source_name: str
    channels: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    created: tuple[str, ...]
    updated: tuple[str, ...]
    disabled: tuple[str, ...]


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


def _coerce_config(config: FarmConfig | str | Path | None) -> FarmConfig:
    return config if isinstance(config, FarmConfig) else load_config(config)


def resolve_channels(
    account: MinerAccount,
    config: FarmConfig | str | Path | None = None,
) -> ChannelResolution:
    """Resolve and validate the exact channels that a fresh launch must use."""

    farm_config = _coerce_config(config)
    if not account.is_configured:
        raise ValidationError(f"Account {account.config_key!r} is not configured.")
    try:
        account_config = farm_config.twitch_users[account.config_key]
    except KeyError as exc:
        raise ValidationError(
            f"Account {account.config_key!r} is missing from config.yaml."
        ) from exc

    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        selection = AccountChannelSelection(account=account, mode=AccountChannelSelection.Mode.DEFAULT)

    if selection.mode == AccountChannelSelection.Mode.DEFAULT:
        channels = normalize_channels(farm_config.default_channels)
        source_name = "config.yaml"
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
        username=account_config.username,
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


@transaction.atomic
def sync_config_accounts(
    config: FarmConfig | str | Path | None = None,
) -> SyncResult:
    """Mirror YAML accounts while preserving existing users' desired state."""

    farm_config = _coerce_config(config)
    now = timezone.now()
    configured_keys = set(farm_config.twitch_users)
    existing = {
        account.config_key: account
        for account in MinerAccount.objects.select_for_update().all()
    }
    created: list[str] = []
    updated: list[str] = []
    disabled: list[str] = []

    for key, account_config in farm_config.twitch_users.items():
        account = existing.get(key)
        if account is None:
            account = MinerAccount.objects.create(
                config_key=key,
                display_username=account_config.username,
                is_configured=True,
                config_synced_at=now,
            )
            AccountChannelSelection.objects.create(account=account)
            MinerInstanceState.objects.create(
                account=account,
                desired_state=(
                    MinerInstanceState.DesiredState.RUNNING
                    if farm_config.autostart_instances
                    else MinerInstanceState.DesiredState.STOPPED
                ),
                observed_state=MinerInstanceState.ObservedState.UNKNOWN,
            )
            created.append(key)
        else:
            account.display_username = account_config.username
            account.is_configured = True
            account.config_synced_at = now
            account.save(
                update_fields=("display_username", "is_configured", "config_synced_at", "updated_at")
            )
            AccountChannelSelection.objects.get_or_create(account=account)
            MinerInstanceState.objects.get_or_create(account=account)
            updated.append(key)

        try:
            fingerprint = resolve_channels(account, farm_config).fingerprint
        except ValidationError:
            fingerprint = ""
        if account.configuration_fingerprint != fingerprint:
            account.configuration_fingerprint = fingerprint
            account.save(update_fields=("configuration_fingerprint", "updated_at"))

    missing_accounts = [account for key, account in existing.items() if key not in configured_keys]
    for account in missing_accounts:
        if account.is_configured:
            account.is_configured = False
            account.configuration_fingerprint = ""
            account.config_synced_at = now
            account.save(
                update_fields=(
                    "is_configured",
                    "configuration_fingerprint",
                    "config_synced_at",
                    "updated_at",
                )
            )
            state, _ = MinerInstanceState.objects.get_or_create(account=account)
            state.desired_state = MinerInstanceState.DesiredState.STOPPED
            state.save(update_fields=("desired_state", "updated_at"))
            disabled.append(account.config_key)

    return SyncResult(tuple(created), tuple(updated), tuple(disabled))


def _fingerprint_without_yaml(account: MinerAccount) -> str:
    """Best-effort fingerprint for web services that cannot read secrets/YAML."""

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
    except (ConfigError, ValidationError):
        fingerprint = _fingerprint_without_yaml(account)
    account.configuration_fingerprint = fingerprint
    account.save(update_fields=("configuration_fingerprint", "updated_at"))


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
    state, _ = MinerInstanceState.objects.select_for_update().get_or_create(account=account)
    pending = MinerCommand.objects.select_for_update().filter(
        account=account,
        status=MinerCommand.Status.QUEUED,
    )

    desired = (
        MinerInstanceState.DesiredState.STOPPED
        if action == MinerCommand.Action.STOP
        else MinerInstanceState.DesiredState.RUNNING
    )
    state.desired_state = desired
    if action == MinerCommand.Action.RESTART:
        state.retry_count = 0
        state.next_retry_at = None
        state.last_error = ""
    state.save(
        update_fields=("desired_state", "retry_count", "next_retry_at", "last_error", "updated_at")
    )

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

    command = pending.filter(action=action).order_by("created_at", "id").first()
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
    for account in MinerAccount.objects.filter(is_configured=True).order_by("config_key"):
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
    *,
    config: FarmConfig | str | Path | None = None,
) -> MinerRun:
    """Validate YAML + DB state and persist an immutable, secret-free launch spec."""

    account = MinerAccount.objects.select_for_update().get(pk=account.pk)
    farm_config = _coerce_config(config)
    resolution = resolve_channels(account, farm_config)
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
