"""Inline keyboards for the Telegram bot."""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.data import DataManager, ChannelPreset


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build main menu keyboard."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👥 Users", callback_data="menu:users"),
        InlineKeyboardButton(text="📋 Presets", callback_data="menu:presets"),
    )
    builder.row(
        InlineKeyboardButton(text="▶️ Start All", callback_data="action:start_all"),
        InlineKeyboardButton(text="⏹ Stop All", callback_data="action:stop_all"),
    )
    return builder.as_markup()


def users_list_keyboard(
    users: dict[str, bool],
    states: dict[str, tuple[str | None, list[str]]],
) -> InlineKeyboardMarkup:
    """
    Build users list keyboard.
    
    Args:
        users: Dict of user_id -> is_running
        states: Dict of user_id -> (assigned_preset, custom_channels)
    """
    builder = InlineKeyboardBuilder()
    
    for user_id, is_running in users.items():
        status = "🟢" if is_running else "🔴"
        preset_name, channels = states.get(user_id, ("__default__", []))
        
        if preset_name == "__default__":
            info = "[Default]"
        elif preset_name == "__custom__":
            info = f"[Custom: {len(channels)} ch]" if channels else "[Custom]"
        else:
            info = f"[{preset_name}]"
        
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {user_id} {info}",
                callback_data=f"user:{user_id}",
            )
        )
    
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main"),
    )
    return builder.as_markup()


def user_control_keyboard(user_id: str, is_running: bool) -> InlineKeyboardMarkup:
    """Build user control keyboard."""
    builder = InlineKeyboardBuilder()
    
    if is_running:
        builder.row(
            InlineKeyboardButton(text="⏹ Stop", callback_data=f"user_action:{user_id}:stop"),
            InlineKeyboardButton(text="🔄 Restart", callback_data=f"user_action:{user_id}:restart"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="▶️ Start", callback_data=f"user_action:{user_id}:start"),
        )
    
    builder.row(
        InlineKeyboardButton(text="📋 Assign Preset", callback_data=f"user_preset:{user_id}"),
        InlineKeyboardButton(text="✏️ Custom Channels", callback_data=f"user_channels:{user_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Back to Users", callback_data="menu:users"),
    )
    return builder.as_markup()


def presets_list_keyboard(presets: list[ChannelPreset]) -> InlineKeyboardMarkup:
    """Build presets list keyboard."""
    builder = InlineKeyboardBuilder()
    
    # System presets
    builder.row(
        InlineKeyboardButton(text="🔄 Default Channels", callback_data="preset:__default__"),
        InlineKeyboardButton(text="✏️ Custom Channels", callback_data="preset:__custom__"),
    )
    
    # User presets
    for preset in presets:
        builder.row(
            InlineKeyboardButton(
                text=f"📋 {preset.name} ({len(preset.channels)} channels)",
                callback_data=f"preset:{preset.name}",
            )
        )
    
    builder.row(
        InlineKeyboardButton(text="➕ Create Preset", callback_data="preset_action:create"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main"),
    )
    return builder.as_markup()


def preset_control_keyboard(preset_name: str) -> InlineKeyboardMarkup:
    """Build preset control keyboard."""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="👥 Assign to Users", callback_data=f"preset_assign:{preset_name}"),
    )
    builder.row(
        InlineKeyboardButton(text="➕ Add Channel", callback_data=f"preset_edit:{preset_name}:add"),
        InlineKeyboardButton(text="➖ Remove Channel", callback_data=f"preset_edit:{preset_name}:remove"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Delete Preset", callback_data=f"preset_edit:{preset_name}:delete"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Back to Presets", callback_data="menu:presets"),
    )
    return builder.as_markup()


def system_preset_keyboard(preset_name: str) -> InlineKeyboardMarkup:
    """Build keyboard for system presets (default/custom)."""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="👥 Assign to Users", callback_data=f"preset_assign:{preset_name}"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Back to Presets", callback_data="menu:presets"),
    )
    return builder.as_markup()


def preset_channels_keyboard(preset_name: str, channels: list[str]) -> InlineKeyboardMarkup:
    """Build keyboard for removing channels from preset."""
    builder = InlineKeyboardBuilder()
    
    for channel in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {channel}",
                callback_data=f"preset_remove_ch:{preset_name}:{channel}",
            )
        )
    
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data=f"preset:{preset_name}"),
    )
    return builder.as_markup()


def assign_preset_keyboard(user_id: str, presets: list[ChannelPreset]) -> InlineKeyboardMarkup:
    """Build keyboard for assigning preset to user."""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(
            text="🔄 Use Default Channels",
            callback_data=f"assign_preset:{user_id}:__default__",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Use Custom Channels",
            callback_data=f"assign_preset:{user_id}:__custom__",
        )
    )
    
    for preset in presets:
        builder.row(
            InlineKeyboardButton(
                text=f"📋 {preset.name}",
                callback_data=f"assign_preset:{user_id}:{preset.name}",
            )
        )
    
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data=f"user:{user_id}"),
    )
    return builder.as_markup()


def preset_users_keyboard(
    preset_name: str,
    users: dict[str, bool],  # user_id -> is_assigned
) -> InlineKeyboardMarkup:
    """Build keyboard for assigning preset to users."""
    builder = InlineKeyboardBuilder()
    
    # Add "Assign to All" button
    builder.row(
        InlineKeyboardButton(
            text="✅ Assign to All",
            callback_data=f"preset_assign_user:{preset_name}:__all__",
        )
    )
    
    for user_id, is_assigned in users.items():
        check = "☑️" if is_assigned else "⬜"
        builder.row(
            InlineKeyboardButton(
                text=f"{check} {user_id}",
                callback_data=f"preset_assign_user:{preset_name}:{user_id}",
            )
        )
    
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data=f"preset:{preset_name}"),
    )
    return builder.as_markup()


def confirm_keyboard(action: str, context: str) -> InlineKeyboardMarkup:
    """Build confirmation keyboard."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm:{action}:{context}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="menu:presets"),
    )
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Build cancel keyboard for input states."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_input"),
    )
    return builder.as_markup()


def add_channels_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for adding channels with Done and Cancel buttons."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Done", callback_data="done_adding_channels"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_input"),
    )
    return builder.as_markup()

