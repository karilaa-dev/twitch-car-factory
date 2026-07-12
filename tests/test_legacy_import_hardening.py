from __future__ import annotations

import io
import pickle
import stat
import struct
import zipfile

import pytest

from controller import legacy_import
from controller.legacy_import import LegacyArchiveError, parse_legacy_archive


_VALID_CONFIG = b"""settings:
  autostart_instances: false
twitch_users:
  primary:
    username: PrimaryUser
    password: secret-password
default_channels:
  - channel_one
"""
_VALID_STATE = b'{"states": []}'


def _archive(
    *,
    config: bytes = _VALID_CONFIG,
    state: bytes = _VALID_STATE,
    cookies: dict[str, bytes] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("config.yaml", config)
        archive.writestr("data/state.json", state)
        for username, payload in (cookies or {}).items():
            archive.writestr(f"cookies/{username}.pkl", payload)
    return output.getvalue()


def _cookie_pickle(cookies: list[dict[str, object]]) -> bytes:
    return pickle.dumps(cookies, protocol=4)


def _assert_cookie_rejected(payload: bytes, message: str) -> None:
    parsed = parse_legacy_archive(
        _archive(cookies={"PrimaryUser": payload})
    )

    assert parsed.cookies == ()
    assert len(parsed.cookie_issues) == 1
    assert parsed.cookie_issues[0].subject == "PrimaryUser"
    assert parsed.cookie_issues[0].code == "invalid_cookie"
    assert message in parsed.cookie_issues[0].message


def test_archive_rejects_encrypted_entry_metadata():
    raw = bytearray(_archive(compression=zipfile.ZIP_STORED))
    local_header = raw.find(b"PK\x03\x04")
    central_header = raw.find(b"PK\x01\x02")
    assert local_header >= 0 and central_header >= 0

    local_flags = struct.unpack_from("<H", raw, local_header + 6)[0]
    central_flags = struct.unpack_from("<H", raw, central_header + 8)[0]
    struct.pack_into("<H", raw, local_header + 6, local_flags | 0x1)
    struct.pack_into("<H", raw, central_header + 8, central_flags | 0x1)

    with pytest.raises(LegacyArchiveError, match="Encrypted ZIP entries"):
        parse_legacy_archive(bytes(raw))


def test_archive_rejects_unsupported_compression():
    pytest.importorskip("bz2")

    with pytest.raises(LegacyArchiveError, match="unsupported compression"):
        parse_legacy_archive(_archive(compression=zipfile.ZIP_BZIP2))


def test_archive_rejects_symbolic_links():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("config.yaml", _VALID_CONFIG)
        archive.writestr("data/state.json", _VALID_STATE)
        link = zipfile.ZipInfo("cookies/PrimaryUser.pkl")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../outside-cookie.pkl")

    with pytest.raises(LegacyArchiveError, match="Symbolic links"):
        parse_legacy_archive(output.getvalue())


def test_archive_rejects_more_than_500_entries():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.yaml", _VALID_CONFIG)
        archive.writestr("data/state.json", _VALID_STATE)
        for index in range(legacy_import.MAX_ARCHIVE_ENTRIES - 1):
            archive.writestr(f"ignored/{index}.txt", b"x")

    with pytest.raises(LegacyArchiveError, match="more than 500 entries"):
        parse_legacy_archive(output.getvalue())


def test_archive_rejects_oversized_config_entry():
    oversized_config = b"x" * (legacy_import.MAX_CONFIG_BYTES + 1)

    with pytest.raises(LegacyArchiveError, match="exceeds its size limit"):
        parse_legacy_archive(
            _archive(config=oversized_config, compression=zipfile.ZIP_STORED)
        )


@pytest.mark.parametrize(
    ("config", "state", "message"),
    [
        (
            b"twitch_users:\n  primary: [unterminated",
            _VALID_STATE,
            "config.yaml is not valid safe YAML",
        ),
        (_VALID_CONFIG, b'{"states": [}', "data/state.json is not valid strict JSON"),
    ],
    ids=("malformed-yaml", "malformed-json"),
)
def test_archive_rejects_malformed_documents(config: bytes, state: bytes, message: str):
    with pytest.raises(LegacyArchiveError, match=message):
        parse_legacy_archive(_archive(config=config, state=state))


def test_cookie_rejects_persistent_references():
    class Sentinel:
        pass

    class PersistentPickler(pickle.Pickler):
        def persistent_id(self, obj):
            if isinstance(obj, Sentinel):
                return "attacker-controlled-reference"
            return None

    output = io.BytesIO()
    PersistentPickler(output, protocol=4).dump(
        [{"name": "auth-token", "value": Sentinel()}]
    )

    _assert_cookie_rejected(output.getvalue(), "not a safe legacy cookie list")


def test_cookie_rejects_missing_auth_token():
    _assert_cookie_rejected(
        _cookie_pickle([{"name": "login", "value": "PrimaryUser"}]),
        "does not contain a non-empty auth-token",
    )


def test_cookie_rejects_login_mismatch():
    _assert_cookie_rejected(
        _cookie_pickle(
            [
                {"name": "login", "value": "AnotherUser"},
                {"name": "auth-token", "value": "token"},
            ]
        ),
        "cookie login does not match its Twitch username",
    )


def test_cookie_rejects_duplicate_names_case_insensitively():
    _assert_cookie_rejected(
        _cookie_pickle(
            [
                {"name": "auth-token", "value": "one"},
                {"name": "AUTH-TOKEN", "value": "two"},
            ]
        ),
        "duplicate cookie names",
    )


def test_cookie_rejects_oversized_value():
    _assert_cookie_rejected(
        _cookie_pickle(
            [
                {
                    "name": "auth-token",
                    "value": "x" * (legacy_import.MAX_COOKIE_VALUE_LENGTH + 1),
                }
            ]
        ),
        "cookie has an invalid value",
    )
