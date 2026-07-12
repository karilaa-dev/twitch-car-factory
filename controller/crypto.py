"""Small application-level encryption boundary for controller secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables


class SecretDecryptionError(ValidationError):
    """Raised when stored encrypted data cannot be opened by the keyring."""


def _keyring() -> tuple[bytes, ...]:
    configured = getattr(settings, "TWITCH_FARM_CREDENTIAL_KEYS", ())
    keys: list[bytes] = []
    for value in configured:
        encoded = value.encode("ascii") if isinstance(value, str) else bytes(value)
        try:
            decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except Exception as exc:  # pragma: no cover - guarded by settings validation
            raise ImproperlyConfigured("Invalid TWITCH_FARM_CREDENTIAL_KEYS value.") from exc
        if len(decoded) != 32:
            raise ImproperlyConfigured(
                "Every TWITCH_FARM_CREDENTIAL_KEYS entry must be a Fernet key."
            )
        keys.append(encoded)
    if not keys:
        raise ImproperlyConfigured("TWITCH_FARM_CREDENTIAL_KEYS must contain a Fernet key.")
    return tuple(keys)


def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in _keyring()])


@sensitive_variables()
def encrypt_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Only text values can be encrypted.")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


@sensitive_variables()
def decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise SecretDecryptionError(
            "Stored credentials cannot be decrypted with the configured keyring."
        ) from exc


@sensitive_variables()
def encrypt_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encrypt_text(encoded)


@sensitive_variables()
def decrypt_json(value: str) -> Any:
    try:
        return json.loads(decrypt_text(value))
    except json.JSONDecodeError as exc:
        raise SecretDecryptionError("Stored encrypted data is not valid JSON.") from exc


@sensitive_variables()
def _digest_payload(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_key(key: bytes) -> bytes:
    decoded = base64.urlsafe_b64decode(key)
    return hashlib.sha256(decoded + b"\0twitch-farm-import-digest-v1").digest()


@sensitive_variables()
def keyed_digest_candidates(value: Any) -> tuple[str, ...]:
    """Return one semantic HMAC per configured credential key.

    New metadata uses the first value.  Verification may accept any candidate,
    so prepending a rotation key does not invalidate drafts or ownership while
    the previous key remains in the decryption keyring.
    """

    encoded = _digest_payload(value)
    return tuple(
        hmac.new(_digest_key(key), encoded, hashlib.sha256).hexdigest()
        for key in _keyring()
    )


@sensitive_variables()
def keyed_digest(value: Any) -> str:
    """Return the primary secret-keyed semantic digest safe for storage."""

    return keyed_digest_candidates(value)[0]


@sensitive_variables()
def keyed_digest_matches(value: Any, expected: str) -> bool:
    """Verify a stored digest with the active or any fallback key."""

    return isinstance(expected, str) and any(
        hmac.compare_digest(candidate, expected)
        for candidate in keyed_digest_candidates(value)
    )
