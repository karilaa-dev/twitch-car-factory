"""Read and validate the non-database Twitch farm configuration.

The YAML file is the sole authority for Twitch usernames, passwords, default
channels, and the one-time autostart seed.  Callers should reload it for every
launch rather than retaining credentials in process-global controller state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

import yaml


CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,100}$")


class ConfigError(ValueError):
    """Raised when ``config.yaml`` cannot safely describe a miner launch."""


@dataclass(frozen=True, slots=True)
class TwitchUserConfig:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FarmConfig:
    twitch_users: dict[str, TwitchUserConfig]
    default_channels: tuple[str, ...]
    autostart_instances: bool
    path: Path

    @property
    def accounts(self) -> dict[str, TwitchUserConfig]:
        """Readable alias for code that does not need the legacy YAML key name."""

        return self.twitch_users


def default_config_path() -> Path:
    configured = os.environ.get("TWITCH_FARM_CONFIG")
    if configured:
        return Path(configured).expanduser()

    # Importing this module must also work in management utilities before Django
    # settings have been initialized.
    try:
        from django.conf import settings

        if settings.configured:
            setting_path = getattr(settings, "TWITCH_FARM_CONFIG", None)
            if setting_path:
                return Path(setting_path).expanduser()
            base_dir = getattr(settings, "BASE_DIR", None)
            if base_dir:
                return Path(base_dir) / "config.yaml"
    except (ImportError, RuntimeError):
        pass

    return Path(__file__).resolve().parent.parent / "config.yaml"


def _require_mapping(value: Any, label: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a YAML mapping.")
    return value


def _normalize_config_channels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError("default_channels must be a YAML list.")

    channels: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ConfigError("Every default channel must be a string.")
        channel = raw.strip()
        if not channel or not CHANNEL_RE.fullmatch(channel):
            raise ConfigError(f"Invalid default channel name: {raw!r}.")
        folded = channel.casefold()
        if folded not in seen:
            channels.append(channel)
            seen.add(folded)
    return tuple(channels)


def load_config(path: str | os.PathLike[str] | Path | None = None) -> FarmConfig:
    """Load a fresh validated configuration without logging any credentials."""

    config_path = Path(path).expanduser() if path is not None else default_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file does not exist: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read configuration file: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Configuration file is not valid YAML: {config_path}") from exc

    data = _require_mapping(raw, "Configuration root")
    users_data = _require_mapping(data.get("twitch_users", {}), "twitch_users")
    twitch_users: dict[str, TwitchUserConfig] = {}
    for raw_key, raw_user in users_data.items():
        key = str(raw_key).strip()
        if not key or len(key) > 150:
            raise ConfigError("Every twitch_users key must be between 1 and 150 characters.")
        user_data = _require_mapping(raw_user, f"twitch_users.{key}")
        username = user_data.get("username")
        password = user_data.get("password")
        if (
            not isinstance(username, str)
            or not username.strip()
            or not CHANNEL_RE.fullmatch(username.strip())
        ):
            raise ConfigError(
                f"twitch_users.{key}.username must be a valid Twitch username."
            )
        if len(username.strip()) > 150:
            raise ConfigError(f"twitch_users.{key}.username is too long.")
        if not isinstance(password, str) or not password:
            raise ConfigError(f"twitch_users.{key}.password must be a non-empty string.")
        twitch_users[key] = TwitchUserConfig(username=username.strip(), password=password)

    settings_data = data.get("settings", {})
    if settings_data is None:
        settings_data = {}
    settings_mapping = _require_mapping(settings_data, "settings")
    autostart = settings_mapping.get("autostart_instances", False)
    if not isinstance(autostart, bool):
        raise ConfigError("settings.autostart_instances must be true or false.")

    return FarmConfig(
        twitch_users=twitch_users,
        default_channels=_normalize_config_channels(data.get("default_channels", [])),
        autostart_instances=autostart,
        path=config_path.resolve(),
    )


def get_account_credentials(
    config_key: str,
    path: str | os.PathLike[str] | Path | None = None,
) -> TwitchUserConfig:
    """Reload YAML and return credentials for one configured account."""

    config = load_config(path)
    try:
        return config.twitch_users[config_key]
    except KeyError as exc:
        raise ConfigError(f"Account {config_key!r} is not present in config.yaml.") from exc
