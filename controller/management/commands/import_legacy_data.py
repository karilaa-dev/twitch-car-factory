"""Import the legacy JSON controller state into SQLite exactly once."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from controller.config import ConfigError, FarmConfig, load_config
from controller.models import (
    AccountChannelSelection,
    AccountCustomChannel,
    ActionLog,
    ChannelPreset,
    MinerAccount,
    MinerInstanceState,
    PresetChannel,
)
from controller.services import normalize_channels, sync_config_accounts


@dataclass(frozen=True, slots=True)
class LegacyPreset:
    name: str
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyState:
    config_key: str
    mode: str
    preset_name: str | None
    custom_channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyPayload:
    presets: tuple[LegacyPreset, ...]
    states: tuple[LegacyState, ...]
    digest: str


def _read_json(
    path: Path,
    label: str,
    *,
    missing_ok: bool = False,
) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        if missing_ok:
            return {}
        raise CommandError(f"Missing legacy {label} file: {path}") from exc
    except OSError as exc:
        raise CommandError(f"Could not read legacy {label} file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CommandError(f"Legacy {label} file is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CommandError(f"Legacy {label} file must contain a JSON object.")
    return value


def _legacy_mode(raw: dict[str, Any]) -> tuple[str, str | None]:
    assigned = raw.get("assigned_preset")
    if assigned is None:
        old_mode = raw.get("channel_mode")
        if old_mode == "custom":
            assigned = "__custom__"
        elif old_mode == "preset" and raw.get("preset"):
            assigned = raw["preset"]
        else:
            assigned = "__default__"

    if assigned == "__default__":
        return AccountChannelSelection.Mode.DEFAULT, None
    if assigned == "__custom__":
        return AccountChannelSelection.Mode.CUSTOM, None
    if not isinstance(assigned, str) or not assigned.strip():
        raise CommandError("Every assigned_preset must be a preset name or a virtual preset.")
    return AccountChannelSelection.Mode.PRESET, assigned.strip()


def _parse_payload(data_dir: Path, config: FarmConfig) -> LegacyPayload:
    # Older installations may have state.json without ever creating a preset.
    # Treat an absent presets.json exactly like {"presets": []}; malformed or
    # unreadable files still fail validation below rather than being ignored.
    preset_document = _read_json(
        data_dir / "presets.json",
        "presets",
        missing_ok=True,
    )
    state_document = _read_json(data_dir / "state.json", "state")
    raw_presets = preset_document.get("presets", [])
    raw_states = state_document.get("states", [])
    if not isinstance(raw_presets, list):
        raise CommandError("Legacy presets.json field 'presets' must be a list.")
    if not isinstance(raw_states, list):
        raise CommandError("Legacy state.json field 'states' must be a list.")

    presets: list[LegacyPreset] = []
    preset_names: dict[str, str] = {}
    for index, raw in enumerate(raw_presets):
        if not isinstance(raw, dict):
            raise CommandError(f"Legacy preset at index {index} must be an object.")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
            raise CommandError(f"Legacy preset at index {index} has an invalid name.")
        clean_name = name.strip()
        folded = clean_name.casefold()
        if folded in preset_names:
            raise CommandError(f"Duplicate legacy preset name: {clean_name}")
        try:
            channels = tuple(normalize_channels(raw.get("channels", [])))
        except ValidationError as exc:
            raise CommandError(f"Invalid channels in preset {clean_name}: {exc.messages[0]}") from exc
        presets.append(LegacyPreset(clean_name, channels))
        preset_names[folded] = clean_name

    states: list[LegacyState] = []
    state_keys: set[str] = set()
    for index, raw in enumerate(raw_states):
        if not isinstance(raw, dict):
            raise CommandError(f"Legacy state at index {index} must be an object.")
        user_id = raw.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id.strip()) > 150:
            raise CommandError(f"Legacy state at index {index} has an invalid user_id.")
        config_key = user_id.strip()
        if config_key in state_keys:
            raise CommandError(f"Duplicate legacy state for account: {config_key}")
        state_keys.add(config_key)
        mode, preset_name = _legacy_mode(raw)
        if preset_name is not None:
            canonical_name = preset_names.get(preset_name.casefold())
            if canonical_name is None:
                raise CommandError(
                    f"Account {config_key} references missing legacy preset {preset_name!r}."
                )
            preset_name = canonical_name
        try:
            custom_channels = tuple(
                normalize_channels(raw.get("custom_channels", []), require_nonempty=False)
            )
        except ValidationError as exc:
            raise CommandError(
                f"Invalid custom channels for account {config_key}: {exc.messages[0]}"
            ) from exc
        if mode == AccountChannelSelection.Mode.CUSTOM and not custom_channels:
            raise CommandError(f"Account {config_key} uses custom mode but has no channels.")
        # Legacy is_running and pid are intentionally neither read nor persisted.
        states.append(LegacyState(config_key, mode, preset_name, custom_channels))

    digest_payload = {
        "config": {
            "accounts": [
                {"key": key, "username": value.username}
                for key, value in sorted(config.twitch_users.items())
            ],
            "default_channels": list(config.default_channels),
            "autostart_instances": config.autostart_instances,
        },
        "presets": [
            {"name": preset.name, "channels": list(preset.channels)} for preset in presets
        ],
        "states": [
            {
                "config_key": state.config_key,
                "mode": state.mode,
                "preset_name": state.preset_name,
                "custom_channels": list(state.custom_channels),
            }
            for state in states
        ],
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return LegacyPayload(tuple(presets), tuple(states), digest)


def _database_matches_payload(payload: LegacyPayload, config: FarmConfig) -> bool:
    """Return true only when an earlier import is still intact."""

    for key, configured in config.twitch_users.items():
        account = MinerAccount.objects.filter(config_key=key).first()
        if (
            account is None
            or not account.is_configured
            or account.display_username != configured.username
        ):
            return False

    preset_map: dict[str, ChannelPreset] = {}
    for legacy in payload.presets:
        preset = ChannelPreset.objects.filter(name=legacy.name).first()
        if preset is None:
            return False
        channels = tuple(
            preset.channels.order_by("position", "id").values_list("name", flat=True)
        )
        if channels != legacy.channels:
            return False
        preset_map[legacy.name.casefold()] = preset

    configured_keys = set(config.twitch_users)
    for legacy in payload.states:
        account = MinerAccount.objects.filter(config_key=legacy.config_key).first()
        if account is None or account.is_configured != (legacy.config_key in configured_keys):
            return False
        try:
            selection = account.selection
        except AccountChannelSelection.DoesNotExist:
            return False
        expected_preset_id = (
            preset_map[legacy.preset_name.casefold()].pk
            if legacy.preset_name is not None
            else None
        )
        if selection.mode != legacy.mode or selection.preset_id != expected_preset_id:
            return False
        custom_channels = tuple(
            account.custom_channels.order_by("position", "id").values_list("name", flat=True)
        )
        if custom_channels != legacy.custom_channels:
            return False
    return True


class Command(BaseCommand):
    help = "Import legacy presets.json and state.json into SQLite."

    def add_arguments(self, parser):
        parser.add_argument("--config", help="Path to config.yaml.")
        parser.add_argument(
            "--data-dir",
            default=str(settings.BASE_DIR / "data"),
            help="Directory containing presets.json and state.json.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate/import inside a transaction and then roll it back.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Replace data from an earlier import when the input digest differs.",
        )

    def _cookie_warnings(self, config: FarmConfig) -> list[str]:
        cookie_dir = Path(getattr(settings, "TWITCH_FARM_COOKIES_DIR", settings.BASE_DIR / "cookies"))
        warnings: list[str] = []
        for account in config.twitch_users.values():
            filename = f"{account.username}.pkl"
            if Path(filename).name != filename:
                warnings.append(f"Unsafe cookie filename for configured user {account.username!r}.")
            elif not (cookie_dir / filename).is_file():
                warnings.append(f"Cookie file not found for configured user {account.username!r}.")
        return warnings

    def handle(self, *args, **options):
        try:
            config = load_config(options.get("config"))
            payload = _parse_payload(Path(options["data_dir"]), config)
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc

        cookie_warnings = self._cookie_warnings(config)
        for warning in cookie_warnings:
            self.stderr.write(self.style.WARNING(f"Warning: {warning}"))
        configured_keys = set(config.twitch_users)
        for state in payload.states:
            if state.config_key not in configured_keys:
                self.stderr.write(
                    self.style.WARNING(
                        f"Warning: preserving state-only account {state.config_key!r} "
                        "as unconfigured."
                    )
                )

        counters = {"presets": 0, "states": 0, "orphans": 0}
        no_op = False
        try:
            with transaction.atomic():
                previous = (
                    ActionLog.objects.select_for_update()
                    .filter(action="legacy_import")
                    .order_by("-created_at", "-id")
                    .first()
                )
                previous_digest = previous.details.get("digest") if previous else None
                if previous_digest == payload.digest and _database_matches_payload(payload, config):
                    no_op = True
                elif previous_digest == payload.digest and not options["replace"]:
                    raise CommandError(
                        "Database state has diverged from the earlier legacy import; rerun with "
                        "--replace after reviewing the changes."
                    )
                elif previous is not None and not options["replace"]:
                    raise CommandError(
                        "Legacy data differs from the previous import; rerun with --replace "
                        "after reviewing the changes."
                    )
                else:
                    sync_config_accounts(config)
                    preset_map: dict[str, ChannelPreset] = {}
                    for legacy in payload.presets:
                        preset = ChannelPreset.objects.filter(name__iexact=legacy.name).first()
                        if preset is None:
                            preset = ChannelPreset.objects.create(name=legacy.name)
                        else:
                            existing_channels = tuple(
                                preset.channels.order_by("position", "id").values_list("name", flat=True)
                            )
                            if existing_channels != legacy.channels and not options["replace"]:
                                raise CommandError(
                                    f"Preset {legacy.name!r} conflicts with existing database data."
                                )
                            preset.name = legacy.name
                            preset.save(update_fields=("name", "updated_at"))
                            preset.channels.all().delete()
                        PresetChannel.objects.bulk_create(
                            PresetChannel(preset=preset, name=name, position=position)
                            for position, name in enumerate(legacy.channels)
                        )
                        preset_map[legacy.name.casefold()] = preset
                        counters["presets"] += 1

                    for legacy in payload.states:
                        account = MinerAccount.objects.filter(config_key=legacy.config_key).first()
                        if account is None:
                            account = MinerAccount.objects.create(
                                config_key=legacy.config_key,
                                display_username=legacy.config_key,
                                is_configured=False,
                            )
                            MinerInstanceState.objects.create(
                                account=account,
                                desired_state=MinerInstanceState.DesiredState.STOPPED,
                                observed_state=MinerInstanceState.ObservedState.UNKNOWN,
                            )
                            AccountChannelSelection.objects.create(account=account)
                        if legacy.config_key not in configured_keys:
                            account.is_configured = False
                            account.save(update_fields=("is_configured", "updated_at"))
                            counters["orphans"] += 1

                        account.custom_channels.all().delete()
                        AccountCustomChannel.objects.bulk_create(
                            AccountCustomChannel(account=account, name=name, position=position)
                            for position, name in enumerate(legacy.custom_channels)
                        )
                        selection, _ = AccountChannelSelection.objects.get_or_create(account=account)
                        selection.mode = legacy.mode
                        selection.preset = (
                            preset_map[legacy.preset_name.casefold()]
                            if legacy.preset_name is not None
                            else None
                        )
                        selection.full_clean()
                        selection.save()
                        account.channel_revision += 1
                        account.configuration_fingerprint = ""
                        account.save(
                            update_fields=(
                                "channel_revision",
                                "configuration_fingerprint",
                                "updated_at",
                            )
                        )
                        counters["states"] += 1

                    ActionLog.objects.create(
                        action="legacy_import",
                        message="Imported legacy JSON controller state.",
                        details={
                            "digest": payload.digest,
                            "preset_count": counters["presets"],
                            "state_count": counters["states"],
                            "orphan_count": counters["orphans"],
                            "config_path": str(config.path),
                            "imported_at": timezone.now().isoformat(),
                        },
                    )

                if options["dry_run"]:
                    transaction.set_rollback(True)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        if no_op:
            self.stdout.write(self.style.SUCCESS("Legacy data is already imported; no changes made."))
            return
        prefix = "Dry run: would import" if options["dry_run"] else "Imported"
        orphan_verb = "preserve" if options["dry_run"] else "preserved"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix} {counters['presets']} preset(s), {counters['states']} account state(s), "
                f"and {orphan_verb} {counters['orphans']} unconfigured account(s)."
            )
        )
