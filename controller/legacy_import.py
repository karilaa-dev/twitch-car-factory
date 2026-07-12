"""Hardened, UI-only importer for legacy Twitch Farm ZIP backups.

The raw archive is inspected and normalized in memory.  Only the normalized
payload is encrypted into a short-lived, actor-bound draft; pickle bytes,
passwords, and cookie values never enter HTML, audit JSON, or application logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
import hmac
import io
import json
import pickle
import pickletools
from pathlib import PurePosixPath
import re
import stat
from typing import Any, BinaryIO, Iterable, Mapping, Sequence
import zipfile

import yaml
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .crypto import (
    SecretDecryptionError,
    decrypt_json,
    decrypt_text,
    encrypt_json,
    encrypt_text,
    keyed_digest,
    keyed_digest_candidates,
    keyed_digest_matches,
)


MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 500
MAX_CONFIG_BYTES = 256 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_COOKIE_BYTES = 256 * 1024
MAX_PATH_DEPTH = 8
MAX_DOCUMENT_DEPTH = 16
MAX_DOCUMENT_NODES = 20_000
MAX_ACCOUNTS = 500
MAX_PRESETS = 1_000
MAX_STATES = 1_000
MAX_CHANNELS = 1_000
MAX_COOKIES = 128
MAX_COOKIE_NAME_LENGTH = 256
MAX_COOKIE_VALUE_LENGTH = 16 * 1024
MAX_COOKIE_VALUE_BYTES = 64 * 1024
MAX_COMPRESSION_RATIO = 200
COMPRESSION_RATIO_MIN_SIZE = 64 * 1024
DRAFT_LIFETIME = timedelta(minutes=30)

SUPPORTED_COMPRESSION = frozenset((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,100}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class LegacyImportError(ValidationError):
    """A safe, user-displayable legacy-import failure."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class LegacyArchiveError(LegacyImportError):
    """The uploaded ZIP or a document inside it is invalid."""


class LegacyImportConflict(LegacyImportError):
    """Applying the normalized data would overwrite UI-owned state."""


class LegacyImportStale(LegacyImportError):
    """The database changed after the user reviewed the preview."""


class LegacyImportReplacementRequired(LegacyImportError):
    """A valid earlier import can only be reconciled explicitly."""


@dataclass(frozen=True, slots=True)
class ImportIssue:
    subject: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"subject": self.subject, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class LegacyConfiguredAccount:
    config_key: str
    username: str
    password: str = field(repr=False)

    def as_secret_dict(self) -> dict[str, str]:
        return {
            "config_key": self.config_key,
            "username": self.username,
            "password": self.password,
        }


@dataclass(frozen=True, slots=True)
class LegacyPreset:
    name: str
    channels: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "channels": list(self.channels)}


@dataclass(frozen=True, slots=True)
class LegacyState:
    config_key: str
    mode: str
    preset_name: str | None
    custom_channels: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_key": self.config_key,
            "mode": self.mode,
            "preset_name": self.preset_name,
            "custom_channels": list(self.custom_channels),
        }


@dataclass(frozen=True, slots=True)
class LegacyCookie:
    username: str
    values: tuple[tuple[str, str], ...] = field(repr=False)

    def as_secret_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "values": [{"name": name, "value": value} for name, value in self.values],
        }

    def normalized_values(self) -> list[dict[str, str]]:
        return [{"name": name, "value": value} for name, value in self.values]


@dataclass(frozen=True, slots=True)
class LegacyPayload:
    accounts: tuple[LegacyConfiguredAccount, ...]
    default_channels: tuple[str, ...]
    autostart_new_accounts: bool
    presets: tuple[LegacyPreset, ...]
    states: tuple[LegacyState, ...]
    cookies: tuple[LegacyCookie, ...]
    cookie_issues: tuple[ImportIssue, ...]
    warnings: tuple[str, ...]
    ignored_files: tuple[str, ...]
    source_digest: str = ""

    def as_secret_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": 1,
            "accounts": [account.as_secret_dict() for account in self.accounts],
            "default_channels": list(self.default_channels),
            "autostart_new_accounts": self.autostart_new_accounts,
            "presets": [preset.as_dict() for preset in self.presets],
            "states": [state.as_dict() for state in self.states],
            "cookies": [cookie.as_secret_dict() for cookie in self.cookies],
            "cookie_issues": [issue.as_dict() for issue in self.cookie_issues],
            "warnings": list(self.warnings),
            "ignored_files": list(self.ignored_files),
        }
        if include_digest:
            value["source_digest"] = self.source_digest
        return value

    @classmethod
    @sensitive_variables()
    def from_secret_dict(cls, value: Any) -> "LegacyPayload":
        if not isinstance(value, dict) or value.get("version") != 1:
            raise LegacyImportError("The encrypted import draft has an unsupported format.")
        try:
            payload = cls(
                accounts=tuple(
                    LegacyConfiguredAccount(
                        config_key=item["config_key"],
                        username=item["username"],
                        password=item["password"],
                    )
                    for item in value["accounts"]
                ),
                default_channels=tuple(value["default_channels"]),
                autostart_new_accounts=value["autostart_new_accounts"],
                presets=tuple(
                    LegacyPreset(name=item["name"], channels=tuple(item["channels"]))
                    for item in value["presets"]
                ),
                states=tuple(
                    LegacyState(
                        config_key=item["config_key"],
                        mode=item["mode"],
                        preset_name=item["preset_name"],
                        custom_channels=tuple(item["custom_channels"]),
                    )
                    for item in value["states"]
                ),
                cookies=tuple(
                    LegacyCookie(
                        username=item["username"],
                        values=tuple(
                            (cookie["name"], cookie["value"])
                            for cookie in item["values"]
                        ),
                    )
                    for item in value["cookies"]
                ),
                cookie_issues=tuple(
                    ImportIssue(
                        subject=item["subject"],
                        code=item["code"],
                        message=item["message"],
                    )
                    for item in value["cookie_issues"]
                ),
                warnings=tuple(value["warnings"]),
                ignored_files=tuple(value["ignored_files"]),
                source_digest=value["source_digest"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LegacyImportError("The encrypted import draft is malformed.") from exc
        if not keyed_digest_matches(
            _payload_semantic(payload),
            payload.source_digest,
        ):
            raise LegacyImportError("The encrypted import draft failed its integrity check.")
        return payload


@dataclass(frozen=True, slots=True)
class DesiredAccount:
    config_key: str
    username: str
    password: str | None = field(default=None, repr=False)
    is_active: bool = False
    mode: str = "default"
    preset_name: str | None = None
    custom_channels: tuple[str, ...] = ()
    cookie: LegacyCookie | None = field(default=None, repr=False)

    def public_row(self, *, message: str = "") -> dict[str, Any]:
        row: dict[str, Any] = {
            "config_key": self.config_key,
            "key": self.config_key,
            "username": self.username,
            "has_cookie": self.cookie is not None,
        }
        if message:
            row["message"] = message
        return row


@dataclass(frozen=True, slots=True)
class ImportOwnership:
    preset_ids: frozenset[int] = frozenset()
    created_preset_ids: frozenset[int] = frozenset()
    account_ids: frozenset[int] = frozenset()
    created_account_ids: frozenset[int] = frozenset()
    mutable_preset_ids: frozenset[int] = frozenset()
    mutable_account_ids: frozenset[int] = frozenset()
    preset_fingerprints: Mapping[int, str] = field(default_factory=dict)
    account_fingerprints: Mapping[int, str] = field(default_factory=dict)
    configuration_owned: bool = False
    configuration_fingerprint: str = ""
    verifiable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "preset_ids": sorted(self.preset_ids),
            "created_preset_ids": sorted(self.created_preset_ids),
            "account_ids": sorted(self.account_ids),
            "created_account_ids": sorted(self.created_account_ids),
            "mutable_preset_ids": sorted(self.mutable_preset_ids),
            "mutable_account_ids": sorted(self.mutable_account_ids),
            # Compatibility aliases for the command-line import released before
            # account terminology was made UI-first.
            "state_account_ids": sorted(self.account_ids),
            "created_state_account_ids": sorted(self.created_account_ids),
            "preset_fingerprints": {
                str(key): value for key, value in sorted(self.preset_fingerprints.items())
            },
            "account_fingerprints": {
                str(key): value for key, value in sorted(self.account_fingerprints.items())
            },
            "configuration_owned": self.configuration_owned,
            "configuration_fingerprint": self.configuration_fingerprint,
            "version": 2,
        }


EMPTY_OWNERSHIP = ImportOwnership()


@dataclass(frozen=True, slots=True)
class ImportPreview:
    accounts_create: tuple[dict[str, Any], ...] = ()
    accounts_update: tuple[dict[str, Any], ...] = ()
    accounts_unchanged: tuple[dict[str, Any], ...] = ()
    accounts_delete: tuple[dict[str, Any], ...] = ()
    accounts_preserve: tuple[dict[str, Any], ...] = ()
    accounts_reset: tuple[dict[str, Any], ...] = ()
    accounts_skipped: tuple[dict[str, Any], ...] = ()
    presets_create: tuple[dict[str, str], ...] = ()
    presets_update: tuple[dict[str, str], ...] = ()
    presets_unchanged: tuple[dict[str, str], ...] = ()
    presets_delete: tuple[dict[str, str], ...] = ()
    presets_preserve: tuple[dict[str, str], ...] = ()
    cookies_import: tuple[dict[str, str], ...] = ()
    cookies_rejected: tuple[dict[str, str], ...] = ()
    settings_changed: bool = False
    default_channels: tuple[str, ...] = ()
    autostart_new_accounts: bool = False
    warnings: tuple[dict[str, str], ...] = ()
    conflicts: tuple[dict[str, str], ...] = ()
    destructive_effects: tuple[dict[str, str], ...] = ()
    ignored_files: tuple[str, ...] = ()
    requires_replace: bool = False
    no_op: bool = False

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, Any]:
        account_groups = {
            "create": list(self.accounts_create),
            "update": list(self.accounts_update),
            "unchanged": list(self.accounts_unchanged),
            "delete": list(self.accounts_delete),
            "preserve": list(self.accounts_preserve),
            "reset": list(self.accounts_reset),
            "skipped": list(self.accounts_skipped),
        }
        preset_groups = {
            "create": list(self.presets_create),
            "update": list(self.presets_update),
            "unchanged": list(self.presets_unchanged),
            "delete": list(self.presets_delete),
            "preserve": list(self.presets_preserve),
        }
        counts = {
            **{f"accounts_{key}": len(value) for key, value in account_groups.items()},
            **{f"presets_{key}": len(value) for key, value in preset_groups.items()},
            "cookies_import": len(self.cookies_import),
            "cookies_rejected": len(self.cookies_rejected),
            "conflicts": len(self.conflicts),
        }
        return {
            "accounts": account_groups,
            "presets": preset_groups,
            "cookies": {
                "import": list(self.cookies_import),
                "rejected": list(self.cookies_rejected),
            },
            "settings": {
                "changed": self.settings_changed,
                "default_channels": list(self.default_channels),
                "autostart_new_accounts": self.autostart_new_accounts,
            },
            "counts": counts,
            "warnings": list(self.warnings),
            "conflicts": list(self.conflicts),
            "destructive_effects": list(self.destructive_effects),
            "ignored_files": list(self.ignored_files),
            "requires_replace": self.requires_replace,
            "can_apply": self.can_apply,
            "no_op": self.no_op,
        }


@dataclass(frozen=True, slots=True)
class ApplyResult:
    no_op: bool
    created_accounts: tuple[str, ...] = ()
    updated_accounts: tuple[str, ...] = ()
    created_presets: tuple[str, ...] = ()
    updated_presets: tuple[str, ...] = ()
    deleted_accounts: tuple[str, ...] = ()
    deleted_presets: tuple[str, ...] = ()
    preserved_accounts: tuple[str, ...] = ()
    preserved_presets: tuple[str, ...] = ()
    skipped_accounts: tuple[dict[str, str], ...] = ()
    warnings: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "no_op": self.no_op,
            "created_accounts": list(self.created_accounts),
            "updated_accounts": list(self.updated_accounts),
            "created_presets": list(self.created_presets),
            "updated_presets": list(self.updated_presets),
            "deleted_accounts": list(self.deleted_accounts),
            "deleted_presets": list(self.deleted_presets),
            "preserved_accounts": list(self.preserved_accounts),
            "preserved_presets": list(self.preserved_presets),
            "skipped_accounts": list(self.skipped_accounts),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class _ArchiveContents:
    files: Mapping[str, bytes]
    ignored_files: tuple[str, ...]


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep=False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise LegacyArchiveError("YAML mapping keys must be scalar values.") from exc
        if duplicate:
            raise LegacyArchiveError(f"Duplicate YAML key: {key!s}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _warning(message: str) -> dict[str, str]:
    return {"message": message}


def _conflict(subject: str, code: str, message: str) -> dict[str, str]:
    return {"subject": subject, "code": code, "message": message}


def _effect(subject: str, message: str) -> dict[str, str]:
    return {"subject": subject, "message": message}


def _read_upload(upload: Any) -> bytes:
    if isinstance(upload, (bytes, bytearray, memoryview)):
        value = bytes(upload)
    elif hasattr(upload, "read"):
        try:
            value = upload.read(MAX_ARCHIVE_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise LegacyArchiveError("The uploaded ZIP could not be read.") from exc
        if not isinstance(value, bytes):
            raise LegacyArchiveError("The uploaded ZIP must be a binary file.")
    else:
        raise LegacyArchiveError("Choose a ZIP backup to import.")
    if not value:
        raise LegacyArchiveError("The uploaded ZIP is empty.")
    if len(value) > MAX_ARCHIVE_BYTES:
        raise LegacyArchiveError("The uploaded ZIP must be 10 MiB or smaller.")
    return value


def _safe_zip_path(filename: str) -> tuple[str, ...]:
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        raise LegacyArchiveError("The ZIP contains an invalid entry name.")
    if "\\" in filename:
        raise LegacyArchiveError("The ZIP contains a non-portable entry path.")
    if filename.startswith("/") or WINDOWS_DRIVE_RE.match(filename):
        raise LegacyArchiveError("The ZIP contains an absolute entry path.")
    raw_parts = filename.split("/")
    parts = tuple(part for part in raw_parts if part != "")
    if any(part in (".", "..") for part in parts):
        raise LegacyArchiveError("The ZIP contains a path-traversal entry.")
    if not parts:
        raise LegacyArchiveError("The ZIP contains an invalid entry path.")
    if len(parts) > MAX_PATH_DEPTH:
        raise LegacyArchiveError("The ZIP contains an excessively nested entry path.")
    return parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _strip_common_wrapper(paths: Sequence[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    path_set = {parts for parts in paths}
    if ("config.yaml",) in path_set and ("data", "state.json") in path_set:
        return tuple(paths)
    wrappers = {
        parts[0]
        for parts in paths
        if len(parts) == 2
        and parts[1] == "config.yaml"
        and (parts[0], "data", "state.json") in path_set
    }
    if len(wrappers) == 1:
        wrapper = wrappers.pop()
        # Safe unknown siblings such as .DS_Store or __MACOSX remain visible
        # to the normal ignored-file preview instead of invalidating a wrapped
        # legacy backup.
        return tuple(
            parts[1:] if len(parts) >= 2 and parts[0] == wrapper else parts
            for parts in paths
        )
    raise LegacyArchiveError(
        "The ZIP must contain config.yaml and data/state.json at its root or inside one common folder."
    )


def _read_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    if info.file_size > limit:
        raise LegacyArchiveError(f"Archive entry {info.filename!r} exceeds its size limit.")
    try:
        with archive.open(info, "r") as handle:
            value = handle.read(limit + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise LegacyArchiveError(f"Archive entry {info.filename!r} could not be read.") from exc
    if len(value) > limit or len(value) != info.file_size:
        raise LegacyArchiveError(f"Archive entry {info.filename!r} has an invalid size.")
    return value


def inspect_legacy_archive(upload: Any) -> _ArchiveContents:
    """Validate a ZIP and return only bounded known-file bytes.

    The function never extracts to the filesystem.  Unknown regular files are
    listed for preview, but still count toward archive-wide entry/size limits.
    """

    raw_archive = _read_upload(upload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_archive), "r", allowZip64=False)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise LegacyArchiveError("The uploaded file is not a valid supported ZIP archive.") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise LegacyArchiveError("The ZIP contains more than 500 entries.")
        file_infos: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        total_size = 0
        raw_seen: set[str] = set()
        for info in infos:
            parts = _safe_zip_path(info.filename)
            normalized_raw = "/".join(parts).casefold()
            if normalized_raw in raw_seen:
                raise LegacyArchiveError("The ZIP contains duplicate normalized entry paths.")
            raw_seen.add(normalized_raw)
            if info.flag_bits & 0x1:
                raise LegacyArchiveError("Encrypted ZIP entries are not supported.")
            if _is_symlink(info):
                raise LegacyArchiveError("Symbolic links are not allowed in legacy ZIPs.")
            if info.is_dir():
                continue
            if info.compress_type not in SUPPORTED_COMPRESSION:
                raise LegacyArchiveError("The ZIP uses an unsupported compression method.")
            total_size += info.file_size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise LegacyArchiveError("The ZIP expands beyond the 25 MiB safety limit.")
            if (
                info.file_size >= COMPRESSION_RATIO_MIN_SIZE
                and info.compress_size == 0
                or (
                    info.file_size >= COMPRESSION_RATIO_MIN_SIZE
                    and info.compress_size > 0
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                )
            ):
                raise LegacyArchiveError("The ZIP contains a suspiciously compressed entry.")
            file_infos.append((info, parts))

        stripped_paths = _strip_common_wrapper([parts for _, parts in file_infos])
        stripped_seen: set[str] = set()
        files: dict[str, bytes] = {}
        ignored: list[str] = []
        for (info, _), parts in zip(file_infos, stripped_paths, strict=True):
            normalized = "/".join(parts)
            folded = normalized.casefold()
            if folded in stripped_seen:
                raise LegacyArchiveError("The ZIP contains duplicate paths after wrapper removal.")
            stripped_seen.add(folded)
            if normalized == "config.yaml":
                limit = MAX_CONFIG_BYTES
            elif normalized in ("data/state.json", "data/presets.json"):
                limit = MAX_JSON_BYTES
            elif (
                len(parts) == 2
                and parts[0] == "cookies"
                and parts[1].endswith(".pkl")
            ):
                limit = MAX_COOKIE_BYTES
            else:
                ignored.append(normalized)
                continue
            files[normalized] = _read_member_bounded(archive, info, limit)

    if "config.yaml" not in files or "data/state.json" not in files:
        raise LegacyArchiveError("The ZIP is missing config.yaml or data/state.json.")
    return _ArchiveContents(files=files, ignored_files=tuple(sorted(ignored)))


def _validate_document_tree(value: Any, label: str) -> None:
    remaining = MAX_DOCUMENT_NODES
    active: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise LegacyArchiveError(f"{label} contains too many values.")
        if depth > MAX_DOCUMENT_DEPTH:
            raise LegacyArchiveError(f"{label} is nested too deeply.")
        if item is None or isinstance(item, (str, bool, int, float)):
            return
        if not isinstance(item, (list, dict)):
            raise LegacyArchiveError(f"{label} contains an unsupported value type.")
        identity = id(item)
        if identity in active:
            raise LegacyArchiveError(f"{label} contains a recursive value.")
        active.add(identity)
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        else:
            for key, child in item.items():
                if not isinstance(key, str):
                    raise LegacyArchiveError(f"{label} mapping keys must be strings.")
                visit(child, depth + 1)
        active.remove(identity)

    visit(value, 0)


def _decode_utf8(value: bytes, label: str) -> str:
    try:
        return value.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LegacyArchiveError(f"{label} must be UTF-8 text.") from exc


def _load_yaml(value: bytes) -> dict[str, Any]:
    text = _decode_utf8(value, "config.yaml")
    try:
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise LegacyArchiveError("config.yaml may not contain YAML aliases or anchors.")
            if isinstance(token, yaml.tokens.TagToken):
                raise LegacyArchiveError("config.yaml may not contain explicit YAML tags.")
        document = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except LegacyImportError:
        raise
    except yaml.YAMLError as exc:
        raise LegacyArchiveError("config.yaml is not valid safe YAML.") from exc
    _validate_document_tree(document, "config.yaml")
    if not isinstance(document, dict):
        raise LegacyArchiveError("config.yaml must contain a YAML mapping.")
    return document


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(value: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(
            _decode_utf8(value, label),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise LegacyArchiveError(f"{label} is not valid strict JSON.") from exc
    _validate_document_tree(document, label)
    if not isinstance(document, dict):
        raise LegacyArchiveError(f"{label} must contain a JSON object.")
    return document


def _normalize_channels(
    value: Any,
    label: str,
    *,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LegacyArchiveError(f"{label} must be a list of channel names.")
    if len(value) > MAX_CHANNELS:
        raise LegacyArchiveError(f"{label} contains too many channels.")
    channels: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise LegacyArchiveError(f"Every value in {label} must be a string.")
        channel = raw.strip()
        if not CHANNEL_RE.fullmatch(channel):
            raise LegacyArchiveError(f"{label} contains an invalid Twitch channel name.")
        folded = channel.casefold()
        if folded not in seen:
            seen.add(folded)
            channels.append(channel)
    if require_nonempty and not channels:
        raise LegacyArchiveError(f"{label} must contain at least one channel.")
    return tuple(channels)


@sensitive_variables()
def _parse_config(
    value: bytes,
) -> tuple[tuple[LegacyConfiguredAccount, ...], tuple[str, ...], bool, list[str]]:
    document = _load_yaml(value)
    warnings: list[str] = []
    unknown_root = sorted(set(document) - {"settings", "twitch_users", "default_channels"})
    if unknown_root:
        warnings.append("Ignored unknown config.yaml fields: " + ", ".join(unknown_root) + ".")

    raw_users = document.get("twitch_users", {})
    if not isinstance(raw_users, dict):
        raise LegacyArchiveError("config.yaml twitch_users must be a mapping.")
    if len(raw_users) > MAX_ACCOUNTS:
        raise LegacyArchiveError("config.yaml contains too many accounts.")
    accounts: list[LegacyConfiguredAccount] = []
    seen_keys: set[str] = set()
    seen_usernames: set[str] = set()
    for raw_key, raw_account in raw_users.items():
        if not isinstance(raw_key, str):
            raise LegacyArchiveError("Every twitch_users key must be text.")
        config_key = raw_key.strip()
        if not config_key or len(config_key) > 150:
            raise LegacyArchiveError("Every twitch_users key must be between 1 and 150 characters.")
        folded_key = config_key.casefold()
        if folded_key in seen_keys:
            raise LegacyArchiveError("config.yaml contains duplicate account keys.")
        seen_keys.add(folded_key)
        if not isinstance(raw_account, dict):
            raise LegacyArchiveError(f"twitch_users.{config_key} must be a mapping.")
        username = raw_account.get("username")
        password = raw_account.get("password")
        if not isinstance(username, str) or not CHANNEL_RE.fullmatch(username.strip()):
            raise LegacyArchiveError(f"twitch_users.{config_key}.username is invalid.")
        clean_username = username.strip()
        folded_username = clean_username.casefold()
        if folded_username in seen_usernames:
            raise LegacyArchiveError("config.yaml contains duplicate Twitch usernames.")
        seen_usernames.add(folded_username)
        if not isinstance(password, str) or not password:
            raise LegacyArchiveError(f"twitch_users.{config_key}.password must not be empty.")
        if len(password) > 4096:
            raise LegacyArchiveError(f"twitch_users.{config_key}.password is too long.")
        unknown = sorted(set(raw_account) - {"username", "password"})
        if unknown:
            warnings.append(
                f"Ignored unknown fields for account {config_key}: {', '.join(unknown)}."
            )
        accounts.append(LegacyConfiguredAccount(config_key, clean_username, password))

    settings_document = document.get("settings", {})
    if settings_document is None:
        settings_document = {}
    if not isinstance(settings_document, dict):
        raise LegacyArchiveError("config.yaml settings must be a mapping.")
    autostart = settings_document.get("autostart_instances", False)
    if not isinstance(autostart, bool):
        raise LegacyArchiveError("settings.autostart_instances must be true or false.")
    unknown_settings = sorted(set(settings_document) - {"autostart_instances"})
    if unknown_settings:
        warnings.append(
            "Ignored unknown config.yaml settings: " + ", ".join(unknown_settings) + "."
        )
    defaults = _normalize_channels(
        document.get("default_channels", []),
        "config.yaml default_channels",
    )
    return tuple(accounts), defaults, autostart, warnings


def _parse_presets(value: bytes | None) -> tuple[tuple[LegacyPreset, ...], list[str]]:
    if value is None:
        return (), []
    document = _load_json(value, "data/presets.json")
    warnings: list[str] = []
    unknown_root = sorted(set(document) - {"presets"})
    if unknown_root:
        warnings.append("Ignored unknown presets.json fields: " + ", ".join(unknown_root) + ".")
    raw_presets = document.get("presets", [])
    if not isinstance(raw_presets, list):
        raise LegacyArchiveError("data/presets.json field 'presets' must be a list.")
    if len(raw_presets) > MAX_PRESETS:
        raise LegacyArchiveError("data/presets.json contains too many presets.")
    presets: list[LegacyPreset] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_presets):
        if not isinstance(raw, dict):
            raise LegacyArchiveError(f"Preset at index {index} must be an object.")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
            raise LegacyArchiveError(f"Preset at index {index} has an invalid name.")
        clean_name = name.strip()
        folded = clean_name.casefold()
        if folded in seen:
            raise LegacyArchiveError(f"Duplicate legacy preset name: {clean_name}.")
        seen.add(folded)
        channels = _normalize_channels(
            raw.get("channels", []),
            f"channels for preset {clean_name}",
            require_nonempty=True,
        )
        unknown = sorted(set(raw) - {"name", "channels"})
        if unknown:
            warnings.append(
                f"Ignored unknown fields for preset {clean_name}: {', '.join(unknown)}."
            )
        presets.append(LegacyPreset(clean_name, channels))
    return tuple(presets), warnings


def _legacy_mode(raw: Mapping[str, Any]) -> tuple[str, str | None]:
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
        return "default", None
    if assigned == "__custom__":
        return "custom", None
    if not isinstance(assigned, str) or not assigned.strip():
        raise LegacyArchiveError(
            "Every assigned_preset must be a preset name or a supported virtual preset."
        )
    return "preset", assigned.strip()


def _parse_states(
    value: bytes,
    presets: Sequence[LegacyPreset],
) -> tuple[tuple[LegacyState, ...], list[str]]:
    document = _load_json(value, "data/state.json")
    warnings: list[str] = []
    unknown_root = sorted(set(document) - {"states"})
    if unknown_root:
        warnings.append("Ignored unknown state.json fields: " + ", ".join(unknown_root) + ".")
    raw_states = document.get("states", [])
    if not isinstance(raw_states, list):
        raise LegacyArchiveError("data/state.json field 'states' must be a list.")
    if len(raw_states) > MAX_STATES:
        raise LegacyArchiveError("data/state.json contains too many account states.")
    preset_names = {preset.name.casefold(): preset.name for preset in presets}
    states: list[LegacyState] = []
    seen: set[str] = set()
    ignored_runtime_fields = False
    for index, raw in enumerate(raw_states):
        if not isinstance(raw, dict):
            raise LegacyArchiveError(f"Account state at index {index} must be an object.")
        user_id = raw.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id.strip()) > 150:
            raise LegacyArchiveError(f"Account state at index {index} has an invalid user_id.")
        config_key = user_id.strip()
        folded = config_key.casefold()
        if folded in seen:
            raise LegacyArchiveError(f"Duplicate legacy state for account {config_key}.")
        seen.add(folded)
        mode, preset_name = _legacy_mode(raw)
        if preset_name is not None:
            canonical = preset_names.get(preset_name.casefold())
            if canonical is None:
                raise LegacyArchiveError(
                    f"Account {config_key} references a missing legacy preset."
                )
            preset_name = canonical
        custom_channels = _normalize_channels(
            raw.get("custom_channels", []),
            f"custom channels for account {config_key}",
            require_nonempty=mode == "custom",
        )
        if "pid" in raw or "is_running" in raw:
            ignored_runtime_fields = True
        unknown = sorted(
            set(raw)
            - {
                "user_id",
                "assigned_preset",
                "channel_mode",
                "preset",
                "custom_channels",
                "pid",
                "is_running",
            }
        )
        if unknown:
            warnings.append(
                f"Ignored unknown fields for account state {config_key}: {', '.join(unknown)}."
            )
        states.append(LegacyState(config_key, mode, preset_name, custom_channels))
    if ignored_runtime_fields:
        warnings.append("Ignored legacy pid and is_running values; imports never start accounts.")
    return tuple(states), warnings


_FORBIDDEN_PICKLE_OPCODES = frozenset(
    {
        "GLOBAL",
        "STACK_GLOBAL",
        "REDUCE",
        "BUILD",
        "OBJ",
        "INST",
        "NEWOBJ",
        "NEWOBJ_EX",
        "EXT1",
        "EXT2",
        "EXT4",
        "PERSID",
        "BINPERSID",
        "NEXT_BUFFER",
        "READONLY_BUFFER",
    }
)


class _RestrictedCookieUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        raise pickle.UnpicklingError("global objects are forbidden")

    def persistent_load(self, pid: Any) -> Any:
        raise pickle.UnpicklingError("persistent references are forbidden")


def _normalize_cookie_pickle(value: bytes, expected_username: str) -> LegacyCookie:
    try:
        for opcode, _, _ in pickletools.genops(value):
            if opcode.name in _FORBIDDEN_PICKLE_OPCODES:
                raise pickle.UnpicklingError("unsafe opcode")
        stream = io.BytesIO(value)
        raw = _RestrictedCookieUnpickler(stream).load()
        if stream.read(1):
            raise pickle.UnpicklingError("trailing pickle data")
    except Exception as exc:
        # Never reflect parser details; malformed pickle exceptions can contain
        # attacker-controlled opcode data.
        raise LegacyArchiveError("The cookie file is not a safe legacy cookie list.") from exc

    if not isinstance(raw, list) or len(raw) > MAX_COOKIES:
        raise LegacyArchiveError("The cookie file is not a bounded list of cookies.")
    active: set[int] = set()
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    value_bytes = 0
    for item in raw:
        identity = id(item)
        if identity in active:
            raise LegacyArchiveError("The cookie file contains a recursive value.")
        active.add(identity)
        if not isinstance(item, dict):
            raise LegacyArchiveError("Every cookie must be a name/value mapping.")
        if set(item) != {"name", "value"}:
            raise LegacyArchiveError("Every cookie must contain only name and value fields.")
        name = item.get("name")
        cookie_value = item.get("value")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_COOKIE_NAME_LENGTH
            or any(character in name for character in "\x00\r\n;")
        ):
            raise LegacyArchiveError("A cookie has an invalid name.")
        if not isinstance(cookie_value, str) or len(cookie_value) > MAX_COOKIE_VALUE_LENGTH:
            raise LegacyArchiveError("A cookie has an invalid value.")
        folded = name.casefold()
        if folded in seen:
            raise LegacyArchiveError("The cookie file contains duplicate cookie names.")
        seen.add(folded)
        value_bytes += len(cookie_value.encode("utf-8"))
        if value_bytes > MAX_COOKIE_VALUE_BYTES:
            raise LegacyArchiveError("The cookie values exceed the safety limit.")
        normalized.append((name, cookie_value))
        active.remove(identity)

    cookie_map = {name.casefold(): value for name, value in normalized}
    token = cookie_map.get("auth-token")
    if not token:
        raise LegacyArchiveError("The cookie file does not contain a non-empty auth-token.")
    login = cookie_map.get("login")
    if login is not None and login.casefold() != expected_username.casefold():
        raise LegacyArchiveError("The cookie login does not match its Twitch username.")
    return LegacyCookie(expected_username, tuple(normalized))


def _parse_cookies(
    files: Mapping[str, bytes],
    accounts: Sequence[LegacyConfiguredAccount],
) -> tuple[tuple[LegacyCookie, ...], tuple[ImportIssue, ...], list[str]]:
    usernames = {account.username.casefold(): account.username for account in accounts}
    cookies: list[LegacyCookie] = []
    issues: list[ImportIssue] = []
    warnings: list[str] = []
    for path, value in sorted(files.items()):
        parts = PurePosixPath(path).parts
        if len(parts) != 2 or parts[0] != "cookies" or not parts[1].endswith(".pkl"):
            continue
        filename_username = parts[1][:-4]
        if not CHANNEL_RE.fullmatch(filename_username):
            issues.append(
                ImportIssue(
                    subject=filename_username or "cookie file",
                    code="invalid_cookie_filename",
                    message="Cookie filename is not a valid Twitch username and was rejected.",
                )
            )
            continue
        canonical_username = usernames.get(filename_username.casefold())
        if canonical_username is None:
            warnings.append(
                f"Ignored cookie for {filename_username}; no configured account uses that username."
            )
            continue
        try:
            cookie = _normalize_cookie_pickle(value, canonical_username)
        except LegacyArchiveError as exc:
            issues.append(
                ImportIssue(
                    subject=canonical_username,
                    code="invalid_cookie",
                    message=str(exc),
                )
            )
        else:
            cookies.append(cookie)
    return tuple(cookies), tuple(issues), warnings


@sensitive_variables()
def _payload_semantic(payload: LegacyPayload) -> dict[str, Any]:
    # The digest is credential-aware but is safe to store because it is keyed
    # with application secret material.  Invalid cookie bytes are deliberately
    # absent: they are discarded and influence only a sanitized issue code.
    semantic = payload.as_secret_dict(include_digest=False)
    semantic.pop("warnings", None)
    semantic.pop("ignored_files", None)
    return semantic


@sensitive_variables()
def _payload_digest(payload: LegacyPayload) -> str:
    return keyed_digest(_payload_semantic(payload))


def parse_legacy_archive(upload: Any) -> LegacyPayload:
    """Inspect and fully normalize an uploaded legacy ZIP without DB writes."""

    archive = inspect_legacy_archive(upload)
    accounts, defaults, autostart, config_warnings = _parse_config(
        archive.files["config.yaml"]
    )
    presets, preset_warnings = _parse_presets(archive.files.get("data/presets.json"))
    states, state_warnings = _parse_states(archive.files["data/state.json"], presets)
    cookies, cookie_issues, cookie_warnings = _parse_cookies(archive.files, accounts)
    configured_keys = {account.config_key.casefold() for account in accounts}
    orphan_warnings = [
        f"State-only account {state.config_key} will be preserved as archived."
        for state in states
        if state.config_key.casefold() not in configured_keys
    ]
    payload = LegacyPayload(
        accounts=accounts,
        default_channels=defaults,
        autostart_new_accounts=autostart,
        presets=presets,
        states=states,
        cookies=cookies,
        cookie_issues=cookie_issues,
        warnings=tuple(
            config_warnings
            + preset_warnings
            + state_warnings
            + cookie_warnings
            + orphan_warnings
        ),
        ignored_files=archive.ignored_files,
    )
    return replace(payload, source_digest=_payload_digest(payload))


def _parse_positive_ids(value: Any) -> frozenset[int] | None:
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value
    ):
        return None
    return frozenset(value)


def _parse_fingerprints(value: Any) -> dict[int, str] | None:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return None
    parsed: dict[int, str] = {}
    for raw_key, digest in value.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            return None
        if (
            key <= 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return None
        parsed[key] = digest
    return parsed


def parse_import_ownership(details: Any) -> ImportOwnership | None:
    """Parse ownership metadata fail-closed.

    Version-one command imports did not include fingerprints or mutable-row
    sets.  Their created rows remain recognizable, but ``verifiable`` is false
    so a UI import will never overwrite them merely because an ID was logged.
    """

    if not isinstance(details, dict) or not isinstance(details.get("ownership"), dict):
        return None
    raw = details["ownership"]
    preset_ids = _parse_positive_ids(raw.get("preset_ids"))
    created_preset_ids = _parse_positive_ids(raw.get("created_preset_ids"))
    account_ids = _parse_positive_ids(raw.get("account_ids", raw.get("state_account_ids")))
    created_account_ids = _parse_positive_ids(
        raw.get("created_account_ids", raw.get("created_state_account_ids"))
    )
    if None in (preset_ids, created_preset_ids, account_ids, created_account_ids):
        return None
    assert preset_ids is not None
    assert created_preset_ids is not None
    assert account_ids is not None
    assert created_account_ids is not None
    if not created_preset_ids <= preset_ids or not created_account_ids <= account_ids:
        return None
    preset_fingerprints = _parse_fingerprints(raw.get("preset_fingerprints"))
    account_fingerprints = _parse_fingerprints(raw.get("account_fingerprints"))
    if preset_fingerprints is None or account_fingerprints is None:
        return None
    mutable_preset_ids = _parse_positive_ids(
        raw.get("mutable_preset_ids", list(created_preset_ids))
    )
    mutable_account_ids = _parse_positive_ids(
        raw.get("mutable_account_ids", list(created_account_ids))
    )
    if mutable_preset_ids is None or mutable_account_ids is None:
        return None
    if not mutable_preset_ids <= preset_ids or not mutable_account_ids <= account_ids:
        return None
    configuration_owned = raw.get("configuration_owned", False)
    configuration_fingerprint = raw.get("configuration_fingerprint", "")
    if not isinstance(configuration_owned, bool) or not isinstance(
        configuration_fingerprint, str
    ):
        return None
    modern = raw.get("version") == 2
    verifiable = modern and (
        set(preset_fingerprints) >= mutable_preset_ids
        and set(account_fingerprints) >= mutable_account_ids
        and (not configuration_owned or bool(configuration_fingerprint))
    )
    return ImportOwnership(
        preset_ids=preset_ids,
        created_preset_ids=created_preset_ids,
        account_ids=account_ids,
        created_account_ids=created_account_ids,
        mutable_preset_ids=mutable_preset_ids,
        mutable_account_ids=mutable_account_ids,
        preset_fingerprints=preset_fingerprints,
        account_fingerprints=account_fingerprints,
        configuration_owned=configuration_owned,
        configuration_fingerprint=configuration_fingerprint,
        verifiable=verifiable,
    )


def _previous_import(*, lock: bool = False):
    from .models import ActionLog

    queryset = ActionLog.objects.filter(action="legacy_import").order_by("-created_at", "-id")
    if lock:
        queryset = queryset.select_for_update()
    log = queryset.first()
    if log is None:
        return None, EMPTY_OWNERSHIP, ""
    ownership = parse_import_ownership(log.details)
    source_digest = ""
    if isinstance(log.details, dict):
        candidate = log.details.get("source_digest", log.details.get("digest", ""))
        if isinstance(candidate, str):
            source_digest = candidate
    return log, ownership, source_digest


def _configuration_values(configuration=None) -> tuple[tuple[str, ...], bool]:
    from .models import FarmConfiguration

    if configuration is None:
        configuration = FarmConfiguration.objects.filter(pk=1).first()
    if configuration is None:
        return (), False
    channels = tuple(str(channel) for channel in configuration.default_channels)
    return channels, bool(configuration.autostart_new_accounts)


def _configuration_semantic(configuration=None) -> dict[str, Any]:
    channels, autostart = _configuration_values(configuration)
    return {"default_channels": list(channels), "autostart_new_accounts": autostart}


def _configuration_fingerprints(configuration=None) -> tuple[str, ...]:
    return keyed_digest_candidates(_configuration_semantic(configuration))


def _configuration_fingerprint(configuration=None) -> str:
    return _configuration_fingerprints(configuration)[0]


def _preset_semantic(preset) -> dict[str, Any]:
    channels = list(
        preset.channels.order_by("position", "id").values_list("name", flat=True)
    )
    return {"name": preset.name, "channels": channels}


def _preset_fingerprints(preset) -> tuple[str, ...]:
    return keyed_digest_candidates(_preset_semantic(preset))


def _preset_fingerprint(preset) -> str:
    return _preset_fingerprints(preset)[0]


def _account_semantic(account) -> dict[str, Any]:
    from .models import AccountCredential, AccountChannelSelection

    credential = AccountCredential.objects.filter(account_id=account.pk).first()
    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        mode = "default"
        preset_name = None
    else:
        mode = selection.mode
        preset_name = selection.preset.name if selection.preset_id else None
    channels = list(
        account.custom_channels.order_by("position", "id").values_list("name", flat=True)
    )
    # Ciphertext is included only inside a keyed digest; it is never exposed.
    return {
        "config_key": account.config_key,
        "username": account.display_username,
        "is_active": account.is_active,
        "credential_ciphertext": (
            credential.password_ciphertext if credential is not None else None
        ),
        "mode": mode,
        "preset_name": preset_name,
        "custom_channels": channels,
    }


def _account_fingerprints(account) -> tuple[str, ...]:
    return keyed_digest_candidates(_account_semantic(account))


def _account_fingerprint(account) -> str:
    return _account_fingerprints(account)[0]


def _desired_account_map(payload: LegacyPayload) -> dict[str, DesiredAccount]:
    states = {state.config_key.casefold(): state for state in payload.states}
    cookies = {cookie.username.casefold(): cookie for cookie in payload.cookies}
    result: dict[str, DesiredAccount] = {}
    configured: set[str] = set()
    for account in payload.accounts:
        folded = account.config_key.casefold()
        configured.add(folded)
        state = states.get(folded)
        result[folded] = DesiredAccount(
            config_key=account.config_key,
            username=account.username,
            password=account.password,
            is_active=True,
            mode=state.mode if state else "default",
            preset_name=state.preset_name if state else None,
            custom_channels=state.custom_channels if state else (),
            cookie=cookies.get(account.username.casefold()),
        )
    for state in payload.states:
        folded = state.config_key.casefold()
        if folded in configured:
            continue
        result[folded] = DesiredAccount(
            config_key=state.config_key,
            username=state.config_key,
            password=None,
            is_active=False,
            mode=state.mode,
            preset_name=state.preset_name,
            custom_channels=state.custom_channels,
        )
    return result


def _existing_accounts_by_key(*, lock: bool = False) -> dict[str, Any]:
    from .models import MinerAccount

    queryset = MinerAccount.objects.all()
    if lock:
        queryset = queryset.select_for_update()
    return {account.config_key.casefold(): account for account in queryset}


def _cookie_policy(
    payload: LegacyPayload,
    desired: dict[str, DesiredAccount],
    existing: Mapping[str, Any],
) -> tuple[dict[str, DesiredAccount], list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    configured_by_username = {
        account.username.casefold(): account.config_key.casefold() for account in payload.accounts
    }
    invalid_by_key: dict[str, ImportIssue] = {}
    rejected: list[dict[str, str]] = []
    for issue in payload.cookie_issues:
        key = configured_by_username.get(issue.subject.casefold())
        if key is None:
            rejected.append(issue.as_dict())
        else:
            invalid_by_key[key] = issue

    kept: dict[str, DesiredAccount] = {}
    skipped: list[dict[str, Any]] = []
    cookie_imports: list[dict[str, str]] = []
    for key, account in desired.items():
        current = existing.get(key)
        issue = invalid_by_key.get(key)
        if issue is not None:
            rejected.append(issue.as_dict())
            if current is None and account.password is not None:
                row = account.public_row(message=issue.message)
                row.update({"subject": account.config_key, "code": issue.code})
                skipped.append(row)
                continue
        if account.cookie is not None:
            if current is None:
                cookie_imports.append(
                    {"subject": account.username, "username": account.username}
                )
            else:
                rejected.append(
                    ImportIssue(
                        subject=account.username,
                        code="cookie_for_existing_account",
                        message="Cookies are accepted only when creating a new account.",
                    ).as_dict()
                )
                account = replace(account, cookie=None)
        kept[key] = account
    return kept, skipped, rejected, cookie_imports


def _selection_values(account) -> tuple[str, str | None, tuple[str, ...]]:
    from .models import AccountChannelSelection

    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        return "default", None, ()
    custom = tuple(
        account.custom_channels.order_by("position", "id").values_list("name", flat=True)
    )
    return (
        selection.mode,
        selection.preset.name if selection.preset_id else None,
        custom,
    )


def _source_summary(mode: str, preset_name: str | None, channels: Sequence[str]) -> str:
    if mode == "preset":
        return f"preset {preset_name or 'missing'}"
    if mode == "custom":
        return f"{len(channels)} custom channel(s)"
    return "farm defaults"


def _account_requires_stop(account) -> bool:
    from .models import MinerInstanceState

    state = MinerInstanceState.objects.filter(account_id=account.pk).only(
        "desired_state", "current_run_id"
    ).first()
    return bool(
        state
        and (
            state.desired_state == MinerInstanceState.DesiredState.RUNNING
            or state.current_run_id
        )
    )


def _account_update_effects(account, wanted: DesiredAccount) -> tuple[str, bool]:
    """Describe every sanitized field/reset effect an account update applies."""

    from .models import AccountCredential

    effects: list[str] = []
    if _account_requires_stop(account):
        effects.append("Running intent will be reset to stopped before changes are applied.")
    if account.display_username != wanted.username:
        effects.append(
            f"Public username changes from {account.display_username} to {wanted.username}."
        )
    if account.is_active != wanted.is_active:
        effects.append("Account becomes active." if wanted.is_active else "Account is archived.")

    has_credential = AccountCredential.objects.filter(account_id=account.pk).exists()
    if wanted.password is None and has_credential:
        effects.append("The encrypted credential will be removed.")
    elif wanted.password is not None:
        effects.append(
            "An encrypted credential will be replaced."
            if has_credential
            else "An encrypted credential will be added."
        )

    current_mode, current_preset, current_channels = _selection_values(account)
    current_source = _source_summary(current_mode, current_preset, current_channels)
    wanted_source = _source_summary(
        wanted.mode,
        wanted.preset_name,
        wanted.custom_channels,
    )
    source_changed = (
        current_mode != wanted.mode
        or (current_preset or "").casefold() != (wanted.preset_name or "").casefold()
        or current_channels != wanted.custom_channels
    )
    if source_changed:
        effects.append(f"Channel source changes from {current_source} to {wanted_source}.")
    return " ".join(effects), source_changed and wanted.mode == "default"


def _account_matches(account, desired: DesiredAccount) -> tuple[bool, bool]:
    """Return (matches, credential_readable)."""

    from .models import AccountCredential

    if account.display_username != desired.username or account.is_active != desired.is_active:
        return False, True
    mode, preset_name, channels = _selection_values(account)
    if (
        mode != desired.mode
        or (preset_name or "").casefold() != (desired.preset_name or "").casefold()
        or channels != desired.custom_channels
    ):
        return False, True
    credential = AccountCredential.objects.filter(account_id=account.pk).first()
    if desired.password is None:
        return credential is None, True
    if credential is None:
        return False, True
    try:
        stored_password = decrypt_text(credential.password_ciphertext)
    except ValidationError:
        return False, False
    return hmac.compare_digest(stored_password, desired.password), True


def _is_legacy_shell(account, desired: DesiredAccount) -> bool:
    """Recognize rows made inert by the UI-managed-account migration."""

    from .models import AccountCredential

    return (
        desired.password is not None
        and not account.is_active
        and account.display_username == desired.username
        and not AccountCredential.objects.filter(account_id=account.pk).exists()
        and not account.configuration_fingerprint
    )


def _account_has_history(account) -> bool:
    return (
        account.runs.exists()
        or account.commands.exists()
        or account.incidents.exists()
        or account.action_logs.exclude(action="legacy_import").exists()
    )


@dataclass(slots=True)
class _DiffPlan:
    preview: ImportPreview
    desired_accounts: dict[str, DesiredAccount]
    account_actions: dict[str, str]
    preset_actions: dict[str, str]
    removed_account_ids: tuple[int, ...]
    preserved_account_ids: tuple[int, ...]
    removed_preset_ids: tuple[int, ...]
    preserved_preset_ids: tuple[int, ...]
    previous_log: Any
    previous_ownership: ImportOwnership
    settings_action: str
    revoked_account_ids: frozenset[int]
    revoked_preset_ids: frozenset[int]
    configuration_revoked: bool


_ACCOUNT_OWNERSHIP_MUTATIONS = frozenset(
    {
        "account_updated",
        "account_archived",
        "account_reactivated",
        "channel_selection_changed",
    }
)
_PRESET_OWNERSHIP_MUTATIONS = frozenset({"preset_updated", "preset_deleted"})


def _ownership_revocations(previous_log) -> tuple[frozenset[int], frozenset[int], bool]:
    """Return rows deliberately touched through the UI after the last import."""

    from .models import ActionLog

    if previous_log is None:
        return frozenset(), frozenset(), False
    account_ids: set[int] = set()
    preset_ids: set[int] = set()
    configuration_revoked = False
    for row in ActionLog.objects.filter(pk__gt=previous_log.pk).values(
        "account_id", "action", "details"
    ):
        action = row["action"]
        if action in _ACCOUNT_OWNERSHIP_MUTATIONS and row["account_id"] is not None:
            account_ids.add(row["account_id"])
        if action in _PRESET_OWNERSHIP_MUTATIONS:
            details = row["details"] if isinstance(row["details"], dict) else {}
            preset_id = details.get("preset_id")
            if isinstance(preset_id, int) and not isinstance(preset_id, bool):
                preset_ids.add(preset_id)
        if action == "farm_settings_updated":
            configuration_revoked = True
    return frozenset(account_ids), frozenset(preset_ids), configuration_revoked


def _current_row_matches_ownership(
    *,
    row_id: int,
    fingerprints: Mapping[int, str],
    current_fingerprints: Sequence[str],
    ownership: ImportOwnership,
    revoked: bool = False,
) -> bool:
    return (
        not revoked
        and ownership.verifiable
        and row_id in fingerprints
        and any(
            hmac.compare_digest(fingerprints[row_id], candidate)
            for candidate in current_fingerprints
        )
    )


def _current_row_is_owned(
    *,
    row_id: int,
    mutable_ids: frozenset[int],
    fingerprints: Mapping[int, str],
    current_fingerprints: Sequence[str],
    ownership: ImportOwnership,
    revoked: bool = False,
) -> bool:
    return row_id in mutable_ids and _current_row_matches_ownership(
        row_id=row_id,
        fingerprints=fingerprints,
        current_fingerprints=current_fingerprints,
        ownership=ownership,
        revoked=revoked,
    )


def _build_diff_plan(payload: LegacyPayload, *, lock: bool = False) -> _DiffPlan:
    from .models import ChannelPreset, FarmConfiguration, MinerAccount

    previous_log, ownership_or_none, previous_digest = _previous_import(lock=lock)
    ownership = ownership_or_none or EMPTY_OWNERSHIP
    revoked_account_ids, revoked_preset_ids, configuration_revoked = (
        _ownership_revocations(previous_log)
    )
    existing_accounts = _existing_accounts_by_key(lock=lock)
    desired = _desired_account_map(payload)
    desired, skipped, cookie_rejections, cookie_imports = _cookie_policy(
        payload, desired, existing_accounts
    )

    conflicts: list[dict[str, str]] = []
    warnings = [_warning(message) for message in payload.warnings]
    account_create: list[dict[str, Any]] = []
    account_update: list[dict[str, Any]] = []
    account_unchanged: list[dict[str, Any]] = []
    account_reset_by_id: dict[int, dict[str, Any]] = {}
    account_actions: dict[str, str] = {}

    def add_account_reset(account, message: str) -> None:
        row = account_reset_by_id.get(account.pk)
        if row is None:
            account_reset_by_id[account.pk] = {
                "config_key": account.config_key,
                "key": account.config_key,
                "username": account.display_username,
                "has_cookie": False,
                "message": message,
            }
        elif message not in row["message"]:
            row["message"] = f"{row['message']} {message}"
    username_owners = {
        account.display_username.casefold(): account for account in existing_accounts.values()
    }
    for key, wanted in desired.items():
        current = existing_accounts.get(key)
        username_owner = username_owners.get(wanted.username.casefold())
        if current is None and username_owner is not None:
            conflicts.append(
                _conflict(
                    wanted.config_key,
                    "username_already_managed",
                    f"Twitch username {wanted.username} is already managed by another account.",
                )
            )
            account_actions[key] = "conflict"
            continue
        if current is None:
            account_create.append(wanted.public_row())
            account_actions[key] = "create"
            continue
        matches, credential_readable = _account_matches(current, wanted)
        if matches:
            account_unchanged.append(wanted.public_row())
            account_actions[key] = "unchanged"
            continue
        owned = _current_row_is_owned(
            row_id=current.pk,
            mutable_ids=ownership.mutable_account_ids,
            fingerprints=ownership.account_fingerprints,
            current_fingerprints=_account_fingerprints(current),
            ownership=ownership,
            revoked=current.pk in revoked_account_ids,
        )
        legacy_shell = _is_legacy_shell(current, wanted)
        if owned or legacy_shell:
            message, resets_source = _account_update_effects(current, wanted)
            if legacy_shell:
                message = f"Legacy account shell will be activated. {message}"
            account_update.append(wanted.public_row(message=message))
            if resets_source:
                add_account_reset(
                    current,
                    "Channel assignment will reset to farm defaults.",
                )
            account_actions[key] = "update"
        else:
            reason = (
                "Stored credentials cannot be verified with the active keyring."
                if not credential_readable
                else "Existing UI-managed data differs from this archive."
            )
            conflicts.append(_conflict(wanted.config_key, "unowned_account", reason))
            account_actions[key] = "conflict"

    existing_presets = {
        preset.name.casefold(): preset
        for preset in (
            ChannelPreset.objects.select_for_update().all()
            if lock
            else ChannelPreset.objects.all()
        )
    }
    preset_create: list[dict[str, str]] = []
    preset_update: list[dict[str, str]] = []
    preset_unchanged: list[dict[str, str]] = []
    preset_actions: dict[str, str] = {}
    for wanted in payload.presets:
        key = wanted.name.casefold()
        current = existing_presets.get(key)
        if current is None:
            preset_create.append({"name": wanted.name})
            preset_actions[key] = "create"
            continue
        current_channels = tuple(
            current.channels.order_by("position", "id").values_list("name", flat=True)
        )
        if current.name == wanted.name and current_channels == wanted.channels:
            preset_unchanged.append({"name": wanted.name})
            preset_actions[key] = "unchanged"
            continue
        if _current_row_is_owned(
            row_id=current.pk,
            mutable_ids=ownership.mutable_preset_ids,
            fingerprints=ownership.preset_fingerprints,
            current_fingerprints=_preset_fingerprints(current),
            ownership=ownership,
            revoked=current.pk in revoked_preset_ids,
        ):
            preset_update.append({"name": wanted.name})
            preset_actions[key] = "update"
        else:
            conflicts.append(
                _conflict(
                    wanted.name,
                    "unowned_preset",
                    f"Preset {wanted.name} already exists with different UI-managed data.",
                )
            )
            preset_actions[key] = "conflict"

    desired_account_ids = {
        existing_accounts[key].pk for key in desired if key in existing_accounts
    }
    removed_account_ids: list[int] = []
    preserved_account_ids: list[int] = []
    account_delete: list[dict[str, Any]] = []
    account_preserve: list[dict[str, Any]] = []
    if previous_log is not None and ownership_or_none is None:
        conflicts.append(
            _conflict(
                "Earlier import",
                "invalid_ownership",
                "The earlier legacy import lacks valid ownership metadata; reconcile it manually.",
            )
        )
    elif previous_log is not None:
        by_id = {
            account.pk: account
            for account in MinerAccount.objects.filter(pk__in=ownership.created_account_ids)
        }
        for account_id in sorted(ownership.created_account_ids - desired_account_ids):
            account = by_id.get(account_id)
            if account is None:
                continue
            expected = ownership.account_fingerprints.get(account_id)
            if (
                not ownership.verifiable
                or account_id in revoked_account_ids
                or not expected
                or not any(
                    hmac.compare_digest(expected, candidate)
                    for candidate in _account_fingerprints(account)
                )
            ):
                conflicts.append(
                    _conflict(
                        account.config_key,
                        "diverged_owned_account",
                        "A previously imported account changed after import and will not be replaced.",
                    )
                )
                continue
            row = {
                "config_key": account.config_key,
                "key": account.config_key,
                "username": account.display_username,
                "has_cookie": False,
            }
            if _account_has_history(account):
                effects = ["History-bearing account will be archived and preserved."]
                if _account_requires_stop(account):
                    effects.append("Running intent will be reset to stopped.")
                mode, preset_name, channels = _selection_values(account)
                if mode != "default" or preset_name is not None or channels:
                    effects.append("Channel assignment will reset to farm defaults.")
                    add_account_reset(
                        account,
                        "Channel assignment will reset to farm defaults while history is preserved.",
                    )
                row["message"] = " ".join(effects)
                account_preserve.append(row)
                preserved_account_ids.append(account_id)
            else:
                row["message"] = "Unused importer-created account will be deleted."
                account_delete.append(row)
                removed_account_ids.append(account_id)

    desired_preset_ids = {
        existing_presets[preset.name.casefold()].pk
        for preset in payload.presets
        if preset.name.casefold() in existing_presets
    }
    removed_preset_ids: list[int] = []
    preserved_preset_ids: list[int] = []
    preset_delete: list[dict[str, str]] = []
    preset_preserve: list[dict[str, str]] = []
    if previous_log is not None and ownership_or_none is not None:
        by_id = {
            preset.pk: preset
            for preset in ChannelPreset.objects.filter(pk__in=ownership.created_preset_ids)
        }
        for preset_id in sorted(ownership.created_preset_ids - desired_preset_ids):
            preset = by_id.get(preset_id)
            if preset is None:
                continue
            expected = ownership.preset_fingerprints.get(preset_id)
            if (
                not ownership.verifiable
                or preset_id in revoked_preset_ids
                or not expected
                or not any(
                    hmac.compare_digest(expected, candidate)
                    for candidate in _preset_fingerprints(preset)
                )
            ):
                conflicts.append(
                    _conflict(
                        preset.name,
                        "diverged_owned_preset",
                        "A previously imported preset changed after import and will not be replaced.",
                    )
                )
                continue
            if preset.account_selections.exists():
                preset_preserve.append({"name": preset.name})
                preserved_preset_ids.append(preset_id)
            else:
                preset_delete.append({"name": preset.name})
                removed_preset_ids.append(preset_id)

    configuration = FarmConfiguration.objects.filter(pk=1).first()
    current_settings = _configuration_values(configuration)
    desired_settings = (payload.default_channels, payload.autostart_new_accounts)
    settings_changed = current_settings != desired_settings
    settings_action = "unchanged"
    if settings_changed:
        configuration_is_default = current_settings == ((), False)
        configuration_is_owned = (
            ownership.configuration_owned
            and not configuration_revoked
            and ownership.verifiable
            and any(
                hmac.compare_digest(ownership.configuration_fingerprint, candidate)
                for candidate in _configuration_fingerprints(configuration)
            )
        )
        if configuration_is_default or configuration_is_owned:
            settings_action = "update"
        else:
            settings_action = "conflict"
            conflicts.append(
                _conflict(
                    "General settings",
                    "unowned_settings",
                    "UI-managed farm settings differ from this archive.",
                )
            )

    if settings_action == "update":
        affected_defaults = MinerAccount.objects.filter(
            is_active=True,
            selection__mode="default",
        )
        if lock:
            affected_defaults = affected_defaults.select_for_update()
        for account in affected_defaults:
            if _account_requires_stop(account):
                add_account_reset(
                    account,
                    "Running intent will be reset to stopped before imported farm defaults are applied.",
                )

    updated_preset_ids = {
        existing_presets[row["name"].casefold()].pk for row in preset_update
    }
    if updated_preset_ids:
        affected_preset_accounts = MinerAccount.objects.filter(
            is_active=True,
            selection__preset_id__in=updated_preset_ids,
        ).select_related("selection__preset")
        if lock:
            affected_preset_accounts = affected_preset_accounts.select_for_update()
        for account in affected_preset_accounts:
            if _account_requires_stop(account):
                add_account_reset(
                    account,
                    f"Running intent will be reset to stopped before preset {account.selection.preset.name} is replaced.",
                )

    account_reset = list(account_reset_by_id.values())

    changes = bool(
        account_create
        or account_update
        or preset_create
        or preset_update
        or account_delete
        or account_preserve
        or account_reset
        or preset_delete
        or settings_action == "update"
    )
    no_op = not changes and not conflicts
    requires_replace = previous_log is not None and changes
    destructive_effects = [
        *(
            _effect(row["config_key"], row.get("message", "Account will be replaced."))
            for row in account_update
            if previous_log is not None
        ),
        *(
            _effect(row["name"], "Preset channels will be replaced.")
            for row in preset_update
        ),
        *(_effect(row["config_key"], row["message"]) for row in account_delete),
        *(_effect(row["config_key"], row["message"]) for row in account_preserve),
        *(_effect(row["config_key"], row["message"]) for row in account_reset),
        *(_effect(row["name"], "Importer-created preset will be deleted.") for row in preset_delete),
        *(
            [_effect("General settings", "Farm defaults will be replaced.")]
            if settings_action == "update" and previous_log is not None
            else []
        ),
    ]
    preview = ImportPreview(
        accounts_create=tuple(account_create),
        accounts_update=tuple(account_update),
        accounts_unchanged=tuple(account_unchanged),
        accounts_delete=tuple(account_delete),
        accounts_preserve=tuple(account_preserve),
        accounts_reset=tuple(account_reset),
        accounts_skipped=tuple(skipped),
        presets_create=tuple(preset_create),
        presets_update=tuple(preset_update),
        presets_unchanged=tuple(preset_unchanged),
        presets_delete=tuple(preset_delete),
        presets_preserve=tuple(preset_preserve),
        cookies_import=tuple(cookie_imports),
        cookies_rejected=tuple(cookie_rejections),
        settings_changed=settings_changed,
        default_channels=payload.default_channels,
        autostart_new_accounts=payload.autostart_new_accounts,
        warnings=tuple(warnings),
        conflicts=tuple(conflicts),
        destructive_effects=tuple(destructive_effects),
        ignored_files=payload.ignored_files,
        requires_replace=requires_replace,
        no_op=no_op,
    )
    return _DiffPlan(
        preview=preview,
        desired_accounts=desired,
        account_actions=account_actions,
        preset_actions=preset_actions,
        removed_account_ids=tuple(removed_account_ids),
        preserved_account_ids=tuple(preserved_account_ids),
        removed_preset_ids=tuple(removed_preset_ids),
        preserved_preset_ids=tuple(preserved_preset_ids),
        previous_log=previous_log,
        previous_ownership=ownership,
        settings_action=settings_action,
        revoked_account_ids=revoked_account_ids,
        revoked_preset_ids=revoked_preset_ids,
        configuration_revoked=configuration_revoked,
    )


def build_import_preview(payload: LegacyPayload) -> ImportPreview:
    """Build a sanitized, non-mutating database diff for a parsed payload."""

    return _build_diff_plan(payload).preview


def _database_snapshot(payload: LegacyPayload, *, lock: bool = False) -> dict[str, Any]:
    """Serialize every row that could affect this payload or replacement."""

    from .models import (
        AccountCredential,
        AccountSessionSeed,
        ChannelPreset,
        FarmConfiguration,
        MinerAccount,
        MinerInstanceState,
    )

    previous_log, ownership, previous_digest = _previous_import(lock=lock)
    owned_account_ids = ownership.account_ids if ownership is not None else frozenset()
    owned_preset_ids = ownership.preset_ids if ownership is not None else frozenset()
    desired = _desired_account_map(payload)
    account_query = MinerAccount.objects.all()
    preset_query = ChannelPreset.objects.all()
    if lock:
        account_query = account_query.select_for_update()
        preset_query = preset_query.select_for_update()
    # The set is deliberately broader than the final touched set so a newly
    # introduced username/key collision invalidates the preview.
    usernames = {item.username.casefold() for item in payload.accounts}
    accounts = [
        account
        for account in account_query
        if account.config_key.casefold() in desired
        or account.display_username.casefold() in usernames
        or account.pk in owned_account_ids
    ]
    credentials = {
        item.account_id: item.password_ciphertext
        for item in AccountCredential.objects.filter(account_id__in=[a.pk for a in accounts])
    }
    seeds = set(
        AccountSessionSeed.objects.filter(account_id__in=[a.pk for a in accounts]).values_list(
            "account_id", flat=True
        )
    )
    states = {
        item.account_id: item
        for item in MinerInstanceState.objects.filter(account_id__in=[a.pk for a in accounts])
    }
    account_snapshot: list[dict[str, Any]] = []
    for account in sorted(accounts, key=lambda item: item.pk):
        mode, preset_name, channels = _selection_values(account)
        state = states.get(account.pk)
        account_snapshot.append(
            {
                "id": account.pk,
                "config_key": account.config_key,
                "username": account.display_username,
                "is_active": account.is_active,
                "channel_revision": account.channel_revision,
                "fingerprint": account.configuration_fingerprint,
                "credential_ciphertext": credentials.get(account.pk),
                "has_session_seed": account.pk in seeds,
                "mode": mode,
                "preset_name": preset_name,
                "custom_channels": list(channels),
                "desired_state": state.desired_state if state else None,
                "observed_state": state.observed_state if state else None,
                "current_run_id": state.current_run_id if state else None,
            }
        )
    desired_preset_names = {preset.name.casefold() for preset in payload.presets}
    presets = [
        preset
        for preset in preset_query
        if preset.name.casefold() in desired_preset_names or preset.pk in owned_preset_ids
    ]
    preset_snapshot = [
        {
            "id": preset.pk,
            "name": preset.name,
            "channels": list(
                preset.channels.order_by("position", "id").values_list("name", flat=True)
            ),
            "selection_count": preset.account_selections.count(),
        }
        for preset in sorted(presets, key=lambda item: item.pk)
    ]
    configuration = FarmConfiguration.objects.filter(pk=1).first()
    channels, autostart = _configuration_values(configuration)
    snapshot = {
        "previous_log_id": previous_log.pk if previous_log is not None else None,
        "previous_source_digest": previous_digest,
        "previous_ownership": ownership.as_dict() if ownership is not None else None,
        "accounts": account_snapshot,
        "presets": preset_snapshot,
        "settings": {
            "exists": configuration is not None,
            "default_channels": list(channels),
            "autostart_new_accounts": autostart,
        },
    }
    return snapshot


def _database_baselines(payload: LegacyPayload, *, lock: bool = False) -> tuple[str, ...]:
    return keyed_digest_candidates(_database_snapshot(payload, lock=lock))


def _database_baseline(payload: LegacyPayload, *, lock: bool = False) -> str:
    return _database_baselines(payload, lock=lock)[0]


@sensitive_variables()
def prepare_legacy_import(upload: Any, actor) -> Any:
    """Create an encrypted, actor-bound 30-minute preview draft."""

    from .models import LegacyImportDraft

    if actor is None or not getattr(actor, "pk", None):
        raise LegacyImportError("A signed-in staff account is required to import legacy data.")
    payload = parse_legacy_archive(upload)
    from .services import purge_expired_legacy_import_drafts

    # SQLite uses BEGIN IMMEDIATE for this project, and row locks cover the
    # portable case.  The rendered diff and its confirmation baseline therefore
    # describe one consistent database snapshot.
    with transaction.atomic():
        purge_expired_legacy_import_drafts()
        preview = _build_diff_plan(payload, lock=True).preview
        baseline = _database_baseline(payload, lock=True)
        return LegacyImportDraft.objects.create(
            actor=actor,
            payload_ciphertext=encrypt_json(payload.as_secret_dict()),
            preview=preview.as_dict(),
            source_digest=payload.source_digest,
            baseline_digest=baseline,
            expires_at=timezone.now() + DRAFT_LIFETIME,
        )


def _write_preset(wanted: LegacyPreset, current=None):
    from .models import ChannelPreset, PresetChannel

    if current is None:
        current = ChannelPreset.objects.create(name=wanted.name)
    else:
        current.name = wanted.name
        current.save(update_fields=("name", "updated_at"))
        current.channels.all().delete()
    PresetChannel.objects.bulk_create(
        PresetChannel(preset=current, name=name, position=position)
        for position, name in enumerate(wanted.channels)
    )
    return current


def _stop_account_for_import(account, *, actor) -> None:
    from .models import MinerCommand, MinerInstanceState
    from .services import enqueue_command

    state, _ = MinerInstanceState.objects.select_for_update().get_or_create(account=account)
    if state.desired_state == MinerInstanceState.DesiredState.RUNNING or state.current_run_id:
        enqueue_command(
            account,
            MinerCommand.Action.STOP,
            actor=actor,
            reason="Account stopped for a confirmed legacy import.",
        )
    else:
        state.desired_state = MinerInstanceState.DesiredState.STOPPED
        state.save(update_fields=("desired_state", "updated_at"))


def _write_account_selection(account, wanted: DesiredAccount, preset_map: Mapping[str, Any]) -> None:
    from .models import AccountChannelSelection, AccountCustomChannel

    account.custom_channels.all().delete()
    AccountCustomChannel.objects.bulk_create(
        AccountCustomChannel(account=account, name=name, position=position)
        for position, name in enumerate(wanted.custom_channels)
    )
    selection, _ = AccountChannelSelection.objects.select_for_update().get_or_create(
        account=account
    )
    selection.mode = wanted.mode
    selection.preset = (
        preset_map[wanted.preset_name.casefold()]
        if wanted.preset_name is not None
        else None
    )
    try:
        selection.full_clean()
    except ValidationError as exc:
        raise LegacyImportError("An imported account has an invalid channel selection.") from exc
    selection.save()


@sensitive_variables()
def _write_desired_account(
    wanted: DesiredAccount,
    *,
    current,
    preset_map: Mapping[str, Any],
    actor,
):
    from .models import (
        AccountCredential,
        AccountSessionSeed,
        MinerAccount,
        MinerInstanceState,
    )

    created = current is None
    username_changed = False
    if created:
        current = MinerAccount.objects.create(
            config_key=wanted.config_key,
            display_username=wanted.username,
            is_active=wanted.is_active,
            configuration_fingerprint="",
        )
        MinerInstanceState.objects.create(
            account=current,
            desired_state=MinerInstanceState.DesiredState.STOPPED,
            observed_state=MinerInstanceState.ObservedState.UNKNOWN,
        )
    else:
        _stop_account_for_import(current, actor=actor)
        username_changed = current.display_username != wanted.username
        current.display_username = wanted.username
        current.is_active = wanted.is_active
        current.configuration_fingerprint = ""
        current.save(
            update_fields=(
                "display_username",
                "is_active",
                "configuration_fingerprint",
                "updated_at",
            )
        )
        if username_changed:
            # A pending seed was validated for the old Twitch identity.  Never
            # carry it across an imported username change.
            AccountSessionSeed.objects.filter(account=current).delete()

    if wanted.password is None:
        AccountCredential.objects.filter(account=current).delete()
    else:
        AccountCredential.objects.update_or_create(
            account=current,
            defaults={"password_ciphertext": encrypt_text(wanted.password)},
        )
    _write_account_selection(current, wanted, preset_map)
    MinerAccount.objects.filter(pk=current.pk).update(
        channel_revision=F("channel_revision") + 1,
        configuration_fingerprint="",
    )
    current.refresh_from_db()
    if created and wanted.cookie is not None:
        AccountSessionSeed.objects.create(
            account=current,
            payload_ciphertext=encrypt_json(wanted.cookie.normalized_values()),
        )
    return current, created


def _archive_preserved_account(account, *, actor) -> None:
    from .models import AccountChannelSelection, MinerAccount

    _stop_account_for_import(account, actor=actor)
    account.custom_channels.all().delete()
    selection, _ = AccountChannelSelection.objects.select_for_update().get_or_create(
        account=account
    )
    selection.mode = AccountChannelSelection.Mode.DEFAULT
    selection.preset = None
    selection.full_clean()
    selection.save()
    account.is_active = False
    account.configuration_fingerprint = ""
    account.save(
        update_fields=("is_active", "configuration_fingerprint", "updated_at")
    )
    MinerAccount.objects.filter(pk=account.pk).update(channel_revision=F("channel_revision") + 1)


@sensitive_variables()
def _apply_diff_plan(payload: LegacyPayload, plan: _DiffPlan, *, actor) -> ApplyResult:
    from .models import (
        ActionLog,
        ChannelPreset,
        FarmConfiguration,
        MinerAccount,
    )

    previous = plan.previous_ownership
    preset_map: dict[str, Any] = {
        preset.name.casefold(): preset
        for preset in ChannelPreset.objects.select_for_update().all()
    }
    created_presets: list[str] = []
    updated_presets: list[str] = []
    created_preset_ids: set[int] = set()
    mutable_preset_ids: set[int] = set()
    for wanted in payload.presets:
        key = wanted.name.casefold()
        action = plan.preset_actions[key]
        if action == "conflict":
            raise LegacyImportConflict("The import preview contains an unresolved preset conflict.")
        if action == "create":
            preset = _write_preset(wanted)
            preset_map[key] = preset
            created_presets.append(wanted.name)
            created_preset_ids.add(preset.pk)
            mutable_preset_ids.add(preset.pk)
        elif action == "update":
            preset = _write_preset(wanted, preset_map[key])
            preset_map[key] = preset
            updated_presets.append(wanted.name)
            if preset.pk in previous.created_preset_ids:
                created_preset_ids.add(preset.pk)
            mutable_preset_ids.add(preset.pk)
        else:
            preset = preset_map[key]
            ownership_intact = _current_row_matches_ownership(
                row_id=preset.pk,
                fingerprints=previous.preset_fingerprints,
                current_fingerprints=_preset_fingerprints(preset),
                ownership=previous,
                revoked=preset.pk in plan.revoked_preset_ids,
            )
            if ownership_intact and preset.pk in previous.created_preset_ids:
                created_preset_ids.add(preset.pk)
            if ownership_intact and preset.pk in previous.mutable_preset_ids:
                mutable_preset_ids.add(preset.pk)

    updated_preset_ids = {
        preset_map[name.casefold()].pk for name in updated_presets
    }
    if updated_preset_ids:
        for account in MinerAccount.objects.select_for_update().filter(
            is_active=True,
            selection__preset_id__in=updated_preset_ids,
        ):
            if _account_requires_stop(account):
                _stop_account_for_import(account, actor=actor)

    existing_accounts = _existing_accounts_by_key(lock=True)
    created_accounts: list[str] = []
    updated_accounts: list[str] = []
    imported_accounts: list[Any] = []
    created_account_ids: set[int] = set()
    mutable_account_ids: set[int] = set()
    for key, wanted in plan.desired_accounts.items():
        action = plan.account_actions[key]
        if action == "conflict":
            raise LegacyImportConflict("The import preview contains an unresolved account conflict.")
        current = existing_accounts.get(key)
        if action in ("create", "update"):
            account, created = _write_desired_account(
                wanted,
                current=current,
                preset_map=preset_map,
                actor=actor,
            )
            existing_accounts[key] = account
            imported_accounts.append(account)
            mutable_account_ids.add(account.pk)
            if created:
                created_accounts.append(wanted.config_key)
                created_account_ids.add(account.pk)
            else:
                updated_accounts.append(wanted.config_key)
                if account.pk in previous.created_account_ids:
                    created_account_ids.add(account.pk)
        else:
            assert current is not None
            imported_accounts.append(current)
            ownership_intact = _current_row_matches_ownership(
                row_id=current.pk,
                fingerprints=previous.account_fingerprints,
                current_fingerprints=_account_fingerprints(current),
                ownership=previous,
                revoked=current.pk in plan.revoked_account_ids,
            )
            if ownership_intact and current.pk in previous.created_account_ids:
                created_account_ids.add(current.pk)
            if ownership_intact and current.pk in previous.mutable_account_ids:
                mutable_account_ids.add(current.pk)

    preserved_accounts: list[str] = []
    for account in MinerAccount.objects.select_for_update().filter(
        pk__in=plan.preserved_account_ids
    ):
        preserved_accounts.append(account.config_key)
        _archive_preserved_account(account, actor=actor)

    deleted_accounts = list(
        MinerAccount.objects.filter(pk__in=plan.removed_account_ids).values_list(
            "config_key", flat=True
        )
    )
    MinerAccount.objects.filter(pk__in=plan.removed_account_ids).delete()

    preserved_presets: list[str] = list(
        ChannelPreset.objects.filter(pk__in=plan.preserved_preset_ids).values_list(
            "name", flat=True
        )
    )
    deleted_presets: list[str] = []
    for preset in ChannelPreset.objects.select_for_update().filter(
        pk__in=plan.removed_preset_ids
    ):
        if preset.account_selections.exists():
            preserved_presets.append(preset.name)
        else:
            deleted_presets.append(preset.name)
            preset.delete()

    if plan.settings_action == "update":
        for account in MinerAccount.objects.select_for_update().filter(
            is_active=True,
            selection__mode="default",
        ):
            if _account_requires_stop(account):
                _stop_account_for_import(account, actor=actor)
        configuration = FarmConfiguration.objects.select_for_update().filter(pk=1).first()
        if configuration is None:
            configuration = FarmConfiguration(pk=1)
        configuration.default_channels = list(payload.default_channels)
        configuration.autostart_new_accounts = payload.autostart_new_accounts
        configuration.save()

    imported_preset_ids = {
        preset_map[preset.name.casefold()].pk for preset in payload.presets
    }
    imported_account_ids = {account.pk for account in imported_accounts}
    preset_fingerprints = {
        preset_id: _preset_fingerprint(ChannelPreset.objects.get(pk=preset_id))
        for preset_id in imported_preset_ids
    }
    account_fingerprints = {
        account_id: _account_fingerprint(MinerAccount.objects.get(pk=account_id))
        for account_id in imported_account_ids
    }
    configuration_ownership_intact = (
        previous.configuration_owned
        and not plan.configuration_revoked
        and previous.verifiable
        and any(
            hmac.compare_digest(previous.configuration_fingerprint, candidate)
            for candidate in _configuration_fingerprints()
        )
    )
    configuration_owned = plan.settings_action == "update" or configuration_ownership_intact
    ownership = ImportOwnership(
        preset_ids=frozenset(imported_preset_ids),
        created_preset_ids=frozenset(created_preset_ids),
        account_ids=frozenset(imported_account_ids),
        created_account_ids=frozenset(created_account_ids),
        mutable_preset_ids=frozenset(mutable_preset_ids),
        mutable_account_ids=frozenset(mutable_account_ids),
        preset_fingerprints=preset_fingerprints,
        account_fingerprints=account_fingerprints,
        configuration_owned=configuration_owned,
        configuration_fingerprint=(
            _configuration_fingerprint() if configuration_owned else ""
        ),
        verifiable=True,
    )
    ActionLog.objects.create(
        actor=actor,
        action="legacy_import",
        message="Imported validated legacy data from Settings.",
        details={
            "source_digest": payload.source_digest,
            "digest": payload.source_digest,
            "imported_at": timezone.now().isoformat(),
            "preset_count": len(imported_preset_ids),
            "account_count": len(imported_account_ids),
            "orphan_count": sum(not account.is_active for account in imported_accounts),
            "skipped_account_count": len(plan.preview.accounts_skipped),
            "rejected_cookie_count": len(plan.preview.cookies_rejected),
            "ownership": ownership.as_dict(),
        },
    )
    return ApplyResult(
        no_op=False,
        created_accounts=tuple(created_accounts),
        updated_accounts=tuple(updated_accounts),
        created_presets=tuple(created_presets),
        updated_presets=tuple(updated_presets),
        deleted_accounts=tuple(deleted_accounts),
        deleted_presets=tuple(deleted_presets),
        preserved_accounts=tuple(preserved_accounts),
        preserved_presets=tuple(dict.fromkeys(preserved_presets)),
        skipped_accounts=plan.preview.accounts_skipped,
        warnings=plan.preview.warnings,
    )


@transaction.atomic
@sensitive_variables()
def confirm_legacy_import(
    draft_or_id: Any,
    actor,
    *,
    replace: bool = False,
    acknowledged: bool = False,
    confirmation: str = "",
) -> ApplyResult:
    """Revalidate and atomically apply one reviewed import draft.

    Confirmation never trusts the stored preview.  The encrypted normalized
    payload is reopened, its keyed digest is checked, and the database baseline
    and complete diff are recomputed under row locks before any application
    writes occur.
    """

    from .models import LegacyImportDraft

    actor_id = getattr(actor, "pk", None)
    if actor_id is None:
        raise LegacyImportError("A signed-in staff account is required to import legacy data.")
    draft_id = getattr(draft_or_id, "pk", draft_or_id)
    try:
        draft = LegacyImportDraft.objects.select_for_update().get(pk=draft_id)
    except (LegacyImportDraft.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise LegacyImportError("The legacy import draft does not exist.") from exc
    if draft.actor_id != actor_id:
        raise LegacyImportError("This legacy import draft belongs to another staff account.")
    if draft.consumed_at is not None:
        raise LegacyImportError("This legacy import draft has already been used.")
    if draft.expires_at <= timezone.now():
        raise LegacyImportError("This legacy import draft expired; upload the ZIP again.")

    try:
        payload = LegacyPayload.from_secret_dict(decrypt_json(draft.payload_ciphertext))
    except SecretDecryptionError as exc:
        raise LegacyImportError(
            "This legacy import draft cannot be decrypted with the configured keyring."
        ) from exc
    if not hmac.compare_digest(payload.source_digest, draft.source_digest):
        raise LegacyImportError("The legacy import draft failed its integrity check.")
    baselines = _database_baselines(payload, lock=True)
    if not any(
        hmac.compare_digest(candidate, draft.baseline_digest)
        for candidate in baselines
    ):
        raise LegacyImportStale(
            "The database changed after this preview was created; upload the ZIP and review it again."
        )
    plan = _build_diff_plan(payload, lock=True)
    if plan.preview.conflicts:
        raise LegacyImportConflict(
            "The import is blocked by UI-owned or unverifiable database conflicts."
        )
    if plan.preview.requires_replace and not replace:
        raise LegacyImportReplacementRequired(
            "This archive changes an earlier import and requires reviewed replacement confirmation."
        )
    if replace and (not acknowledged or confirmation.strip() != "REPLACE"):
        raise LegacyImportReplacementRequired(
            "Acknowledge the replacement effects and type REPLACE to continue."
        )

    if plan.preview.no_op:
        result = ApplyResult(
            no_op=True,
            skipped_accounts=plan.preview.accounts_skipped,
            warnings=plan.preview.warnings,
        )
    else:
        try:
            result = _apply_diff_plan(payload, plan, actor=actor)
        except IntegrityError as exc:
            raise LegacyImportConflict(
                "The database changed while the import was being applied; no changes were committed."
            ) from exc
    draft.consumed_at = timezone.now()
    # Retain only the sanitized preview and single-use marker.  Passwords and
    # cookie values are removed immediately after a successful confirmation.
    draft.payload_ciphertext = encrypt_json({"consumed": True})
    draft.save(update_fields=("consumed_at", "payload_ciphertext"))
    return result


# Clear, view-friendly aliases.
create_legacy_import_draft = prepare_legacy_import
apply_legacy_import = confirm_legacy_import
