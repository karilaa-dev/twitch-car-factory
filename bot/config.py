"""Configuration loader for the Twitch Farm Bot."""

import yaml
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class TwitchUserConfig:
    """Configuration for a single Twitch user."""
    username: str
    password: str


@dataclass
class TelegramConfig:
    """Telegram bot configuration."""
    bot_token: str
    whitelist: list[int] = field(default_factory=list)


@dataclass
class SettingsConfig:
    """Bot settings configuration."""
    autostart_instances: bool = False


@dataclass
class AppConfig:
    """Main application configuration."""
    telegram: TelegramConfig
    twitch_users: dict[str, TwitchUserConfig]
    default_channels: list[str]
    settings: SettingsConfig

    @classmethod
    def load(cls, config_path: Path | None = None) -> "AppConfig":
        """Load configuration from YAML file."""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config.yaml"

        with open(config_path, "r") as f:
            data = yaml.safe_load(f)

        telegram = TelegramConfig(
            bot_token=data["telegram"]["bot_token"],
            whitelist=data["telegram"].get("whitelist", []),
        )

        settings_data = data.get("settings", {})
        settings = SettingsConfig(
            autostart_instances=settings_data.get("autostart_instances", False),
        )

        twitch_users = {}
        for user_id, user_data in data.get("twitch_users", {}).items():
            twitch_users[user_id] = TwitchUserConfig(
                username=user_data["username"],
                password=user_data["password"],
            )

        return cls(
            telegram=telegram,
            twitch_users=twitch_users,
            default_channels=data.get("default_channels", []),
            settings=settings,
        )

