from __future__ import annotations

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings

from controller.crypto import SecretDecryptionError, decrypt_text, encrypt_text
from controller.models import AccountCredential, ActionLog
from controller.services import create_account, get_account_password
from twitch_farm import settings as project_settings


def _new_key() -> str:
    return Fernet.generate_key().decode("ascii")


def test_encrypt_text_uses_the_first_fernet_key() -> None:
    primary_key = _new_key()
    fallback_key = _new_key()
    plaintext = "first-key-only-secret"

    with override_settings(
        TWITCH_FARM_CREDENTIAL_KEYS=(primary_key, fallback_key),
    ):
        ciphertext = encrypt_text(plaintext)

    assert Fernet(primary_key).decrypt(ciphertext.encode("ascii")).decode("utf-8") == plaintext
    with pytest.raises(InvalidToken):
        Fernet(fallback_key).decrypt(ciphertext.encode("ascii"))


def test_decrypt_text_accepts_an_old_key_after_rotation() -> None:
    old_key = _new_key()
    new_key = _new_key()
    plaintext = "rotation-fallback-secret"

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(old_key,)):
        ciphertext = encrypt_text(plaintext)

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(new_key, old_key)):
        assert decrypt_text(ciphertext) == plaintext


def test_decrypt_text_fails_closed_with_the_wrong_key() -> None:
    original_key = _new_key()
    wrong_key = _new_key()
    plaintext = "wrong-key-secret"

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(original_key,)):
        ciphertext = encrypt_text(plaintext)

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(wrong_key,)):
        with pytest.raises(SecretDecryptionError) as captured:
            decrypt_text(ciphertext)

    rendered_error = f"{captured.value!s}\n{captured.value!r}"
    assert plaintext not in rendered_error
    assert ciphertext not in rendered_error
    assert "configured keyring" in rendered_error


def test_production_settings_require_a_credential_key(monkeypatch) -> None:
    monkeypatch.setattr(project_settings, "DEBUG", False)
    monkeypatch.delenv("TWITCH_FARM_CREDENTIAL_KEYS", raising=False)

    with pytest.raises(ImproperlyConfigured, match="at least one Fernet key"):
        project_settings.credential_keys()


@pytest.mark.parametrize(
    "configured",
    (
        "not-base64!",
        base64.urlsafe_b64encode(b"too-short").decode("ascii"),
    ),
)
def test_production_settings_reject_invalid_credential_keys(
    monkeypatch,
    configured: str,
) -> None:
    monkeypatch.setattr(project_settings, "DEBUG", False)
    monkeypatch.setenv("TWITCH_FARM_CREDENTIAL_KEYS", configured)

    with pytest.raises(ImproperlyConfigured, match="invalid Fernet key"):
        project_settings.credential_keys()


@pytest.mark.django_db
def test_account_password_is_absent_from_sqlite_audits_and_decryption_errors() -> None:
    password = "sqlite-audit-error-secret-91c5c63a"
    account = create_account(
        config_key="encrypted-account",
        username="EncryptedUser",
        password=password,
    )
    credential = AccountCredential.objects.get(account=account)

    connection.ensure_connection()
    sqlite_dump = "\n".join(connection.connection.iterdump())
    audit_dump = json.dumps(
        list(ActionLog.objects.filter(account=account).values()),
        default=str,
        sort_keys=True,
    )

    assert credential.password_ciphertext in sqlite_dump
    assert password not in credential.password_ciphertext
    assert password not in sqlite_dump
    assert password not in audit_dump

    with override_settings(TWITCH_FARM_CREDENTIAL_KEYS=(_new_key(),)):
        with pytest.raises(SecretDecryptionError) as captured:
            get_account_password(account)

    rendered_error = f"{captured.value!s}\n{captured.value!r}"
    assert password not in rendered_error
    assert credential.password_ciphertext not in rendered_error

