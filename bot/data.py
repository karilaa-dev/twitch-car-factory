"""Data management for presets and state."""

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any


DATA_DIR = Path(__file__).parent.parent / "data"
PRESETS_FILE = DATA_DIR / "presets.json"
STATE_FILE = DATA_DIR / "state.json"


@dataclass
class ChannelPreset:
    """A preset containing a list of channels to farm."""
    name: str
    channels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelPreset":
        return cls(
            name=data["name"],
            channels=data.get("channels", []),
        )


@dataclass
class UserState:
    """State for a single Twitch user."""
    user_id: str
    is_running: bool = False
    assigned_preset: str = "__default__"  # "__default__", "__custom__", or preset name
    custom_channels: list[str] = field(default_factory=list)
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserState":
        # Migration from old format
        assigned_preset = data.get("assigned_preset")
        if assigned_preset is None:
            # Check for old channel_mode field
            channel_mode = data.get("channel_mode")
            if channel_mode == "custom":
                assigned_preset = "__custom__"
            elif channel_mode == "preset" and data.get("assigned_preset"):
                assigned_preset = data.get("assigned_preset")
            else:
                assigned_preset = "__default__"
        
        return cls(
            user_id=data["user_id"],
            is_running=data.get("is_running", False),
            assigned_preset=assigned_preset,
            custom_channels=data.get("custom_channels", []),
            pid=data.get("pid"),
        )


class DataManager:
    """Manages presets and user states."""

    def __init__(self):
        self._ensure_data_dir()
        self._presets: dict[str, ChannelPreset] = {}
        self._states: dict[str, UserState] = {}
        self._load()

    def _ensure_data_dir(self) -> None:
        """Ensure data directory exists."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        """Load data from JSON files."""
        # Load presets
        if PRESETS_FILE.exists():
            with open(PRESETS_FILE, "r") as f:
                data = json.load(f)
                for preset_data in data.get("presets", []):
                    preset = ChannelPreset.from_dict(preset_data)
                    self._presets[preset.name] = preset

        # Load states
        if STATE_FILE.exists():
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                for state_data in data.get("states", []):
                    state = UserState.from_dict(state_data)
                    self._states[state.user_id] = state

    def _save_presets(self) -> None:
        """Save presets to JSON file."""
        data = {"presets": [p.to_dict() for p in self._presets.values()]}
        with open(PRESETS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _save_states(self) -> None:
        """Save states to JSON file."""
        data = {"states": [s.to_dict() for s in self._states.values()]}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    # Preset operations
    def get_presets(self) -> list[ChannelPreset]:
        """Get all presets."""
        return list(self._presets.values())

    def get_preset(self, name: str) -> ChannelPreset | None:
        """Get a preset by name."""
        return self._presets.get(name)

    def create_preset(self, name: str, channels: list[str]) -> ChannelPreset:
        """Create a new preset."""
        preset = ChannelPreset(name=name, channels=channels)
        self._presets[name] = preset
        self._save_presets()
        return preset

    def update_preset(self, name: str, channels: list[str]) -> ChannelPreset | None:
        """Update an existing preset."""
        if name not in self._presets:
            return None
        self._presets[name].channels = channels
        self._save_presets()
        return self._presets[name]

    def delete_preset(self, name: str) -> bool:
        """Delete a preset."""
        if name not in self._presets:
            return False
        del self._presets[name]
        self._save_presets()
        return True

    def add_channel_to_preset(self, preset_name: str, channel: str) -> bool:
        """Add a channel to a preset."""
        if preset_name not in self._presets:
            return False
        if channel not in self._presets[preset_name].channels:
            self._presets[preset_name].channels.append(channel)
            self._save_presets()
        return True

    def remove_channel_from_preset(self, preset_name: str, channel: str) -> bool:
        """Remove a channel from a preset."""
        if preset_name not in self._presets:
            return False
        if channel in self._presets[preset_name].channels:
            self._presets[preset_name].channels.remove(channel)
            self._save_presets()
        return True

    # State operations
    def get_user_state(self, user_id: str) -> UserState:
        """Get state for a user, creating if needed."""
        if user_id not in self._states:
            self._states[user_id] = UserState(user_id=user_id)
            self._save_states()
        return self._states[user_id]

    def get_all_states(self) -> list[UserState]:
        """Get all user states."""
        return list(self._states.values())

    def set_user_running(self, user_id: str, is_running: bool, pid: int | None = None) -> None:
        """Set user running state."""
        state = self.get_user_state(user_id)
        state.is_running = is_running
        state.pid = pid
        self._save_states()

    def assign_preset_to_user(self, user_id: str, preset_name: str) -> bool:
        """Assign a preset (or virtual preset) to a user."""
        # Allow virtual presets and real presets
        if preset_name not in ("__default__", "__custom__") and preset_name not in self._presets:
            return False
        state = self.get_user_state(user_id)
        state.assigned_preset = preset_name
        self._save_states()
        return True

    def set_user_mode_custom(self, user_id: str) -> bool:
        """Set user to use custom channels."""
        state = self.get_user_state(user_id)
        if not state.custom_channels:
            return False
        state.assigned_preset = "__custom__"
        self._save_states()
        return True

    def set_user_mode_default(self, user_id: str) -> None:
        """Set user to use default channels."""
        state = self.get_user_state(user_id)
        state.assigned_preset = "__default__"
        self._save_states()

    def set_user_custom_channels(self, user_id: str, channels: list[str]) -> None:
        """Set custom channels for a user and switch to custom mode."""
        state = self.get_user_state(user_id)
        state.assigned_preset = "__custom__"
        state.custom_channels = channels
        self._save_states()

    def get_user_channels(self, user_id: str, default_channels: list[str]) -> list[str]:
        """Get the channels a user should farm based on assigned_preset."""
        state = self.get_user_state(user_id)
        
        if state.assigned_preset == "__default__":
            return default_channels
        
        if state.assigned_preset == "__custom__":
            if state.custom_channels:
                return state.custom_channels
            return default_channels
        
        # Regular preset
        if state.assigned_preset in self._presets:
            return self._presets[state.assigned_preset].channels
        
        # Preset not found, fall back to default
        return default_channels

    def initialize_users(self, user_ids: list[str]) -> None:
        """Initialize states for all configured users."""
        for user_id in user_ids:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
        self._save_states()

