"""Shared context, FSM states, constants, and helper functions."""

from functools import wraps
from typing import Callable

from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from bot.keyboards import main_menu_keyboard, user_control_keyboard, presets_list_keyboard


# Constants for special preset names
PRESET_DEFAULT = "__default__"
PRESET_CUSTOM = "__custom__"
USER_ALL = "__all__"


# FSM States
class PresetStates(StatesGroup):
    waiting_preset_name = State()
    waiting_channel_name = State()


class UserStates(StatesGroup):
    waiting_custom_channels = State()


# Dependency holder (set from main.py)
class BotContext:
    config = None
    data = None
    process_manager = None
    health_monitor = None
    bot = None


def is_whitelisted(user_id: int) -> bool:
    """Check if user is in whitelist."""
    return user_id in BotContext.config.telegram.whitelist


def require_whitelist(handler: Callable) -> Callable:
    """Decorator to check whitelist for callback handlers."""
    @wraps(handler)
    async def wrapper(event: CallbackQuery | Message, *args, **kwargs):
        user_id = event.from_user.id
        if not is_whitelisted(user_id):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Not authorized", show_alert=True)
            else:
                await event.answer("⛔ You are not authorized to use this bot.")
            return
        return await handler(event, *args, **kwargs)
    return wrapper


def escape_md(text: str) -> str:
    """Escape Markdown special characters."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


# Notification helpers

async def notify_user(telegram_id: int, message: str) -> None:
    """Send notification to a specific telegram user."""
    if not BotContext.bot:
        return
    try:
        await BotContext.bot.send_message(telegram_id, message, parse_mode="Markdown")
    except Exception:
        pass


async def notify_all(message: str) -> None:
    """Send notification to all whitelisted users."""
    for telegram_user_id in BotContext.config.telegram.whitelist:
        await notify_user(telegram_user_id, message)


# Restart helpers

async def restart_instance(user_id: str, notify_telegram_id: int | None = None) -> bool:
    """Restart a user instance and send notification.
    
    Args:
        user_id: The twitch user to restart
        notify_telegram_id: If provided, only notify this user. Otherwise notify all.
    
    Returns:
        True if restart was successful
    """
    if not BotContext.process_manager.is_running(user_id):
        return False
    
    BotContext.process_manager.stop_instance(user_id)
    success = BotContext.process_manager.start_instance(user_id)
    
    if success and BotContext.health_monitor:
        BotContext.health_monitor.reset_user(user_id)
    
    # Send notification
    msg = f"✅ Instance `{user_id}` restarted successfully" if success else f"❌ Failed to restart instance `{user_id}`"
    if notify_telegram_id is not None:
        await notify_user(notify_telegram_id, msg)
    else:
        await notify_all(msg)
    
    return success


async def restart_if_running(user_id: str, notify_telegram_id: int | None = None) -> bool:
    """Restart a user if running. Returns True if was running."""
    if BotContext.process_manager.is_running(user_id):
        await restart_instance(user_id, notify_telegram_id)
        return True
    return False


async def restart_users_with_preset(preset_name: str, notify_telegram_id: int | None = None) -> int:
    """Restart all running users with a given preset. Returns count restarted."""
    restarted = 0
    for user_id in BotContext.config.twitch_users:
        user_state = BotContext.data.get_user_state(user_id)
        if user_state.assigned_preset == preset_name:
            if await restart_if_running(user_id, notify_telegram_id):
                restarted += 1
    return restarted


# Status helpers

def get_users_status() -> tuple[dict[str, bool], dict[str, tuple[str | None, list[str]]]]:
    """Get users running status and their channel assignments."""
    status = BotContext.process_manager.get_all_status()
    states = {
        user_id: (
            BotContext.data.get_user_state(user_id).assigned_preset,
            BotContext.data.get_user_state(user_id).custom_channels,
        )
        for user_id in status
    }
    return status, states


def get_user_source_line(user_state) -> str:
    """Get the source line for user display based on assigned_preset."""
    if user_state.assigned_preset == PRESET_DEFAULT:
        channels = BotContext.config.default_channels
        channels_text = ", ".join(escape_md(ch) for ch in channels[:5])
        if len(channels) > 5:
            channels_text += f" (+{len(channels) - 5} more)"
        return f"Default: {channels_text}"
    
    if user_state.assigned_preset == PRESET_CUSTOM:
        if user_state.custom_channels:
            channels_text = ", ".join(escape_md(ch) for ch in user_state.custom_channels[:5])
            if len(user_state.custom_channels) > 5:
                channels_text += f" (+{len(user_state.custom_channels) - 5} more)"
            return f"Custom: {channels_text}"
        return "Custom: (no channels set)"
    
    return f"Preset: {escape_md(user_state.assigned_preset)}"


# View refresh helpers

def build_main_menu_text() -> str:
    """Build main menu text with status."""
    status, _ = get_users_status()
    running = sum(1 for v in status.values() if v)
    total = len(status)
    return (
        "🎮 **Twitch Farm Controller**\n\n"
        f"📊 Status: {running}/{total} instances running\n\n"
        "Choose an option:"
    )


def build_user_view_text(user_id: str) -> str:
    """Build user control panel text."""
    is_running = BotContext.process_manager.is_running(user_id)
    user_state = BotContext.data.get_user_state(user_id)
    status_emoji = "🟢 Running" if is_running else "🔴 Stopped"
    source_line = get_user_source_line(user_state)
    return (
        f"👤 **User: {escape_md(user_id)}**\n\n"
        f"Status: {status_emoji}\n"
        f"{source_line}\n"
    )


async def refresh_main_menu(callback: CallbackQuery) -> None:
    """Refresh main menu view, ignoring 'not modified' errors."""
    try:
        await callback.message.edit_text(
            build_main_menu_text(),
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def refresh_user_view(callback: CallbackQuery, user_id: str) -> None:
    """Refresh user control panel view."""
    is_running = BotContext.process_manager.is_running(user_id)
    await callback.message.edit_text(
        build_user_view_text(user_id),
        reply_markup=user_control_keyboard(user_id, is_running),
        parse_mode="Markdown",
    )


async def refresh_presets_list(callback: CallbackQuery) -> None:
    """Refresh presets list view."""
    presets = BotContext.data.get_presets()
    await callback.message.edit_text(
        "📋 **Channel Presets**\n\nSelect a preset to manage or create a new one:",
        reply_markup=presets_list_keyboard(presets),
        parse_mode="Markdown",
    )

