"""Telegram bot handlers."""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from bot.keyboards import (
    main_menu_keyboard,
    users_list_keyboard,
    user_control_keyboard,
    presets_list_keyboard,
    preset_control_keyboard,
    system_preset_keyboard,
    preset_channels_keyboard,
    preset_users_keyboard,
    assign_preset_keyboard,
    confirm_keyboard,
    cancel_keyboard,
    add_channels_keyboard,
)


router = Router()


# FSM States
class PresetStates(StatesGroup):
    waiting_preset_name = State()
    waiting_channel_name = State()


class UserStates(StatesGroup):
    waiting_custom_channels = State()


# Dependency holders (set from main)
class BotContext:
    config = None
    data = None
    process_manager = None
    health_monitor = None
    bot = None


def is_whitelisted(user_id: int) -> bool:
    """Check if user is in whitelist."""
    return user_id in BotContext.config.telegram.whitelist


def escape_md(text: str) -> str:
    """Escape Markdown special characters."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


async def notify_all(message: str) -> None:
    """Send notification to all whitelisted users."""
    if not BotContext.bot:
        return
    for telegram_user_id in BotContext.config.telegram.whitelist:
        try:
            await BotContext.bot.send_message(telegram_user_id, message, parse_mode="Markdown")
        except Exception:
            pass


async def notify_user(telegram_id: int, message: str) -> None:
    """Send notification to a specific telegram user."""
    if not BotContext.bot:
        return
    try:
        await BotContext.bot.send_message(telegram_id, message, parse_mode="Markdown")
    except Exception:
        pass


async def restart_user_with_notification(user_id: str, trigger_telegram_id: int | None = None) -> bool:
    """Restart a user instance and send notification when launched.
    
    If trigger_telegram_id is provided, only notify that user.
    Otherwise, notify all whitelisted users.
    """
    if not BotContext.process_manager.is_running(user_id):
        return False
    
    # Stop the instance
    BotContext.process_manager.stop_instance(user_id)
    
    # Start the instance
    success = BotContext.process_manager.start_instance(user_id)
    
    # Choose notification method based on trigger
    if trigger_telegram_id is not None:
        notify = lambda msg: notify_user(trigger_telegram_id, msg)
    else:
        notify = notify_all
    
    if success:
        if BotContext.health_monitor:
            BotContext.health_monitor.reset_user(user_id)
        await notify(f"✅ Instance `{user_id}` restarted successfully")
    else:
        await notify(f"❌ Failed to restart instance `{user_id}`")
    
    return success


async def restart_users_with_preset(preset_name: str, trigger_telegram_id: int | None = None) -> int:
    """Restart all running users who have this preset assigned. Returns count of restarted."""
    restarted = 0
    for user_id in BotContext.config.twitch_users:
        user_state = BotContext.data.get_user_state(user_id)
        if user_state.assigned_preset == preset_name and BotContext.process_manager.is_running(user_id):
            if await restart_user_with_notification(user_id, trigger_telegram_id):
                restarted += 1
    return restarted


async def do_restart_if_running(user_id: str, trigger_telegram_id: int | None = None) -> bool:
    """Restart a user if running and send notification. Returns True if was running."""
    if BotContext.process_manager.is_running(user_id):
        await restart_user_with_notification(user_id, trigger_telegram_id)
        return True
    return False


def get_user_source_line(user_state) -> str:
    """Get the source line for user display based on assigned_preset."""
    if user_state.assigned_preset == "__default__":
        channels = BotContext.config.default_channels
        channels_text = ", ".join(escape_md(ch) for ch in channels[:5])
        if len(channels) > 5:
            channels_text += f" (+{len(channels) - 5} more)"
        return f"Default: {channels_text}"
    elif user_state.assigned_preset == "__custom__":
        if user_state.custom_channels:
            channels_text = ", ".join(escape_md(ch) for ch in user_state.custom_channels[:5])
            if len(user_state.custom_channels) > 5:
                channels_text += f" (+{len(user_state.custom_channels) - 5} more)"
            return f"Custom: {channels_text}"
        return "Custom: (no channels set)"
    else:
        return f"Preset: {escape_md(user_state.assigned_preset)}"


def get_users_status() -> tuple[dict[str, bool], dict[str, tuple[str | None, list[str]]]]:
    """Get users status and their channel assignments."""
    status = BotContext.process_manager.get_all_status()
    states = {}
    for user_id in status:
        user_state = BotContext.data.get_user_state(user_id)
        states[user_id] = (user_state.assigned_preset, user_state.custom_channels)
    return status, states


# Command handlers
@router.message(Command("start"))
async def cmd_start(message: Message):
    """Handle /start command."""
    if not is_whitelisted(message.from_user.id):
        await message.answer("⛔ You are not authorized to use this bot.")
        return

    status, states = get_users_status()
    running = sum(1 for v in status.values() if v)
    total = len(status)

    text = (
        "🎮 **Twitch Farm Controller**\n\n"
        f"📊 Status: {running}/{total} instances running\n\n"
        "Choose an option:"
    )
    await message.answer(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")


# Menu navigation callbacks
@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, state: FSMContext):
    """Show main menu."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    await state.clear()
    status, states = get_users_status()
    running = sum(1 for v in status.values() if v)
    total = len(status)

    text = (
        "🎮 **Twitch Farm Controller**\n\n"
        f"📊 Status: {running}/{total} instances running\n\n"
        "Choose an option:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "menu:users")
async def menu_users(callback: CallbackQuery):
    """Show users list."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    status, states = get_users_status()
    
    text = "👥 **Twitch Users**\n\n🟢 = running, 🔴 = stopped\n\nSelect a user to manage:"
    await callback.message.edit_text(
        text,
        reply_markup=users_list_keyboard(status, states),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("user:"))
async def show_user(callback: CallbackQuery):
    """Show user control panel."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    user_id = callback.data.split(":")[1]
    is_running = BotContext.process_manager.is_running(user_id)
    user_state = BotContext.data.get_user_state(user_id)

    status_emoji = "🟢 Running" if is_running else "🔴 Stopped"
    source_line = get_user_source_line(user_state)

    text = (
        f"👤 **User: {escape_md(user_id)}**\n\n"
        f"Status: {status_emoji}\n"
        f"{source_line}\n"
    )
    await callback.message.edit_text(
        text,
        reply_markup=user_control_keyboard(user_id, is_running),
        parse_mode="Markdown",
    )
    await callback.answer()


# User action callbacks
@router.callback_query(F.data.startswith("user_action:"))
async def user_action(callback: CallbackQuery):
    """Handle user start/stop/restart actions."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    parts = callback.data.split(":")
    user_id = parts[1]
    action = parts[2]

    if action == "start":
        success = BotContext.process_manager.start_instance(user_id)
        if success and BotContext.health_monitor:
            BotContext.health_monitor.reset_user(user_id)
        msg = f"✅ Started {user_id}" if success else f"❌ Failed to start {user_id}"
        await callback.answer(msg, show_alert=True)
    elif action == "stop":
        success = BotContext.process_manager.stop_instance(user_id)
        msg = f"✅ Stopped {user_id}" if success else f"❌ Failed to stop {user_id}"
        await callback.answer(msg, show_alert=True)
    elif action == "restart":
        await callback.answer(f"🔄 Restarting {user_id}...", show_alert=True)
        await restart_user_with_notification(user_id, callback.from_user.id)
    else:
        await callback.answer("Unknown action", show_alert=True)
        return
    
    # Refresh user view
    is_running = BotContext.process_manager.is_running(user_id)
    user_state = BotContext.data.get_user_state(user_id)

    status_emoji = "🟢 Running" if is_running else "🔴 Stopped"
    
    source_line = get_user_source_line(user_state)

    text = (
        f"👤 **User: {escape_md(user_id)}**\n\n"
        f"Status: {status_emoji}\n"
        f"{source_line}\n"
    )
    await callback.message.edit_text(
        text,
        reply_markup=user_control_keyboard(user_id, is_running),
        parse_mode="Markdown",
    )


# Global actions
@router.callback_query(F.data == "action:start_all")
async def action_start_all(callback: CallbackQuery):
    """Start all instances."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    results = BotContext.process_manager.start_all()
    started = sum(1 for v in results.values() if v)
    
    # Reset health monitor state for successfully started users
    if BotContext.health_monitor:
        for user_id, success in results.items():
            if success:
                BotContext.health_monitor.reset_user(user_id)
    
    await callback.answer(f"✅ Started {started}/{len(results)} instances", show_alert=True)
    
    # Refresh main menu
    status, states = get_users_status()
    running = sum(1 for v in status.values() if v)
    total = len(status)

    text = (
        "🎮 **Twitch Farm Controller**\n\n"
        f"📊 Status: {running}/{total} instances running\n\n"
        "Choose an option:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data == "action:stop_all")
async def action_stop_all(callback: CallbackQuery):
    """Stop all instances."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    BotContext.process_manager.stop_all()
    await callback.answer("✅ Stopped all instances", show_alert=True)
    
    # Refresh main menu
    status, states = get_users_status()
    running = sum(1 for v in status.values() if v)
    total = len(status)

    text = (
        "🎮 **Twitch Farm Controller**\n\n"
        f"📊 Status: {running}/{total} instances running\n\n"
        "Choose an option:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# Preset menu
@router.callback_query(F.data == "menu:presets")
async def menu_presets(callback: CallbackQuery, state: FSMContext):
    """Show presets list."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    await state.clear()
    presets = BotContext.data.get_presets()
    text = "📋 **Channel Presets**\n\nSelect a preset to manage or create a new one:"
    await callback.message.edit_text(
        text,
        reply_markup=presets_list_keyboard(presets),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset:"))
async def show_preset(callback: CallbackQuery):
    """Show preset details."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    preset_name = callback.data.split(":", 1)[1]
    
    # Handle system presets
    if preset_name == "__default__":
        channels = BotContext.config.default_channels
        channels_text = "\n".join(f"  • {ch}" for ch in channels) or "  (no channels)"
        text = (
            "🔄 **Default Channels**\n\n"
            f"Channels:\n{channels_text}"
        )
        await callback.message.edit_text(
            text,
            reply_markup=system_preset_keyboard("__default__"),
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    
    if preset_name == "__custom__":
        text = (
            "✏️ **Custom Channels**\n\n"
            "Each user has their own custom channels.\n"
            "Assign this to users who have custom channels set."
        )
        await callback.message.edit_text(
            text,
            reply_markup=system_preset_keyboard("__custom__"),
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    
    # Regular preset
    preset = BotContext.data.get_preset(preset_name)
    
    if not preset:
        await callback.answer("Preset not found", show_alert=True)
        return

    channels_text = "\n".join(f"  • {ch}" for ch in preset.channels) or "  (no channels)"
    text = (
        f"📋 **Preset: {preset.name}**\n\n"
        f"Channels:\n{channels_text}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=preset_control_keyboard(preset_name),
        parse_mode="Markdown",
    )
    await callback.answer()


# Preset creation
@router.callback_query(F.data == "preset_action:create")
async def preset_create_start(callback: CallbackQuery, state: FSMContext):
    """Start preset creation flow."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    await state.set_state(PresetStates.waiting_preset_name)
    await callback.message.edit_text(
        "📝 **Create New Preset**\n\nEnter a name for the new preset:",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(StateFilter(PresetStates.waiting_preset_name))
async def preset_create_name(message: Message, state: FSMContext):
    """Handle preset name input."""
    if not is_whitelisted(message.from_user.id):
        return

    name = message.text.strip()
    if not name:
        await message.answer("Please enter a valid name.")
        return

    if BotContext.data.get_preset(name):
        await message.answer("A preset with this name already exists. Please choose another name.")
        return

    await state.update_data(preset_name=name)
    await state.set_state(PresetStates.waiting_channel_name)
    await message.answer(
        f"📝 **Preset: {name}**\n\n"
        "Enter channels to add (one per line or comma-separated).\n"
        "Press Done when finished:",
        reply_markup=add_channels_keyboard(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(PresetStates.waiting_channel_name))
async def preset_add_channels(message: Message, state: FSMContext):
    """Handle channel input for preset."""
    if not is_whitelisted(message.from_user.id):
        return

    data = await state.get_data()
    preset_name = data.get("preset_name")
    channels = data.get("channels", [])

    text = message.text.strip().lower()
    edit_mode = data.get("edit_mode", False)
    
    if text == "done":
        if not channels:
            await message.answer("Please add at least one channel before finishing.")
            return
        
        if edit_mode:
            # Add channels to existing preset
            for ch in channels:
                BotContext.data.add_channel_to_preset(preset_name, ch)
            restarted = await restart_users_with_preset(preset_name, message.from_user.id)
            restart_msg = f" Restarted {restarted} user(s)." if restarted else ""
            await state.clear()
            
            preset = BotContext.data.get_preset(preset_name)
            channels_text = "\n".join(f"  • {ch}" for ch in preset.channels) or "  (no channels)"
            await message.answer(
                f"✅ Added {len(channels)} channel(s) to '{preset_name}'!{restart_msg}\n\n"
                f"Channels:\n{channels_text}",
                reply_markup=preset_control_keyboard(preset_name),
            )
        else:
            # Create new preset
            BotContext.data.create_preset(preset_name, channels)
            await state.clear()
            
            presets = BotContext.data.get_presets()
            await message.answer(
                f"✅ Preset '{preset_name}' created with {len(channels)} channels!",
                reply_markup=presets_list_keyboard(presets),
            )
        return

    # Parse channels
    new_channels = [ch.strip() for ch in text.replace(",", "\n").split("\n") if ch.strip()]
    channels.extend(new_channels)
    await state.update_data(channels=channels)
    
    await message.answer(
        f"Added {len(new_channels)} channel(s). Total: {len(channels)}\n"
        "Add more or press Done to finish.",
        reply_markup=add_channels_keyboard(),
    )


@router.callback_query(F.data == "done_adding_channels", StateFilter(PresetStates.waiting_channel_name))
async def done_adding_channels(callback: CallbackQuery, state: FSMContext):
    """Handle Done button when adding channels."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    data = await state.get_data()
    preset_name = data.get("preset_name")
    channels = data.get("channels", [])
    edit_mode = data.get("edit_mode", False)

    if not channels:
        await callback.answer("Please add at least one channel before finishing.", show_alert=True)
        return

    if edit_mode:
        # Add channels to existing preset
        for ch in channels:
            BotContext.data.add_channel_to_preset(preset_name, ch)
        restarted = await restart_users_with_preset(preset_name, callback.from_user.id)
        restart_msg = f" Restarted {restarted} user(s)." if restarted else ""
        await state.clear()
        
        preset = BotContext.data.get_preset(preset_name)
        channels_text = "\n".join(f"  • {ch}" for ch in preset.channels) or "  (no channels)"
        await callback.message.edit_text(
            f"✅ Added {len(channels)} channel(s) to '{preset_name}'!{restart_msg}\n\n"
            f"Channels:\n{channels_text}",
            reply_markup=preset_control_keyboard(preset_name),
        )
    else:
        # Create new preset
        BotContext.data.create_preset(preset_name, channels)
        await state.clear()
        
        presets = BotContext.data.get_presets()
        await callback.message.edit_text(
            f"✅ Preset '{preset_name}' created with {len(channels)} channels!",
            reply_markup=presets_list_keyboard(presets),
        )
    
    await callback.answer()


# Preset editing
@router.callback_query(F.data.startswith("preset_edit:"))
async def preset_edit(callback: CallbackQuery, state: FSMContext):
    """Handle preset edit actions."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    parts = callback.data.split(":")
    preset_name = parts[1]
    action = parts[2]

    if action == "add":
        await state.set_state(PresetStates.waiting_channel_name)
        await state.update_data(preset_name=preset_name, channels=[], edit_mode=True)
        await callback.message.edit_text(
            f"📝 **Add channels to: {preset_name}**\n\n"
            "Enter channels (one per line or comma-separated).\n"
            "Press Done when finished:",
            reply_markup=add_channels_keyboard(),
            parse_mode="Markdown",
        )
        await callback.answer()

    elif action == "remove":
        preset = BotContext.data.get_preset(preset_name)
        if not preset or not preset.channels:
            await callback.answer("No channels to remove", show_alert=True)
            return
        
        await callback.message.edit_text(
            f"📝 **Remove channels from: {preset_name}**\n\nTap a channel to remove it:",
            reply_markup=preset_channels_keyboard(preset_name, preset.channels),
            parse_mode="Markdown",
        )
        await callback.answer()

    elif action == "delete":
        await callback.message.edit_text(
            f"⚠️ **Delete preset '{preset_name}'?**\n\nThis cannot be undone.",
            reply_markup=confirm_keyboard("delete_preset", preset_name),
            parse_mode="Markdown",
        )
        await callback.answer()


@router.callback_query(F.data.startswith("preset_remove_ch:"))
async def preset_remove_channel(callback: CallbackQuery):
    """Remove a channel from preset."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    preset_name = parts[1]
    channel = parts[2]

    BotContext.data.remove_channel_from_preset(preset_name, channel)
    restarted = await restart_users_with_preset(preset_name, callback.from_user.id)
    restart_msg = f" ({restarted} restarted)" if restarted else ""
    await callback.answer(f"Removed {channel}{restart_msg}")

    preset = BotContext.data.get_preset(preset_name)
    if preset and preset.channels:
        await callback.message.edit_text(
            f"📝 **Remove channels from: {preset_name}**\n\nTap a channel to remove it:",
            reply_markup=preset_channels_keyboard(preset_name, preset.channels),
            parse_mode="Markdown",
        )
    else:
        # No more channels, go back to preset view
        presets = BotContext.data.get_presets()
        await callback.message.edit_text(
            "📋 **Channel Presets**\n\nSelect a preset to manage or create a new one:",
            reply_markup=presets_list_keyboard(presets),
            parse_mode="Markdown",
        )


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirm(callback: CallbackQuery):
    """Handle confirmation callbacks."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    action = parts[1]
    context = parts[2]

    if action == "delete_preset":
        BotContext.data.delete_preset(context)
        await callback.answer(f"✅ Preset '{context}' deleted", show_alert=True)
        
        presets = BotContext.data.get_presets()
        await callback.message.edit_text(
            "📋 **Channel Presets**\n\nSelect a preset to manage or create a new one:",
            reply_markup=presets_list_keyboard(presets),
            parse_mode="Markdown",
        )


# Preset -> assign to users
@router.callback_query(F.data.startswith("preset_assign:"))
async def preset_assign_menu(callback: CallbackQuery):
    """Show user selection for preset assignment."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    preset_name = callback.data.split(":", 1)[1]
    
    # Handle system presets
    if preset_name == "__default__":
        users = {}
        for user_id in BotContext.config.twitch_users:
            user_state = BotContext.data.get_user_state(user_id)
            users[user_id] = user_state.assigned_preset == "__default__"
        
        await callback.message.edit_text(
            "👥 **Assign 'Default Channels' to users**\n\nTap a user to toggle:",
            reply_markup=preset_users_keyboard(preset_name, users),
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    
    if preset_name == "__custom__":
        users = {}
        for user_id in BotContext.config.twitch_users:
            user_state = BotContext.data.get_user_state(user_id)
            users[user_id] = user_state.assigned_preset == "__custom__"
        
        await callback.message.edit_text(
            "👥 **Assign 'Custom Channels' to users**\n\nTap a user to toggle:",
            reply_markup=preset_users_keyboard(preset_name, users),
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    
    # Regular preset
    preset = BotContext.data.get_preset(preset_name)
    
    if not preset:
        await callback.answer("Preset not found", show_alert=True)
        return

    # Build user -> is_assigned mapping
    users = {}
    for user_id in BotContext.config.twitch_users:
        user_state = BotContext.data.get_user_state(user_id)
        users[user_id] = user_state.assigned_preset == preset_name

    await callback.message.edit_text(
        f"👥 **Assign '{escape_md(preset_name)}' to users**\n\nTap a user to toggle assignment:",
        reply_markup=preset_users_keyboard(preset_name, users),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_assign_user:"))
async def preset_assign_user(callback: CallbackQuery):
    """Toggle preset assignment for a user."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    preset_name = parts[1]
    user_id = parts[2]

    # Handle system presets
    if preset_name == "__default__":
        if user_id == "__all__":
            for uid in BotContext.config.twitch_users:
                BotContext.data.set_user_mode_default(uid)
                await do_restart_if_running(uid, callback.from_user.id)
            await callback.answer("✅ All users set to default", show_alert=True)
        else:
            user_state = BotContext.data.get_user_state(user_id)
            if user_state.assigned_preset == "__default__":
                await callback.answer("Already using default")
            else:
                BotContext.data.set_user_mode_default(user_id)
                await do_restart_if_running(user_id, callback.from_user.id)
                await callback.answer(f"Set {user_id} to default")
        
        # Refresh
        users = {}
        for uid in BotContext.config.twitch_users:
            user_state = BotContext.data.get_user_state(uid)
            users[uid] = user_state.assigned_preset == "__default__"
        await callback.message.edit_text(
            "👥 **Assign 'Default Channels' to users**\n\nTap a user to toggle:",
            reply_markup=preset_users_keyboard(preset_name, users),
            parse_mode="Markdown",
        )
        return
    
    if preset_name == "__custom__":
        if user_id == "__all__":
            count = 0
            for uid in BotContext.config.twitch_users:
                if BotContext.data.set_user_mode_custom(uid):
                    count += 1
                    await do_restart_if_running(uid, callback.from_user.id)
            await callback.answer(f"✅ Set {count} users to custom", show_alert=True)
        else:
            user_state = BotContext.data.get_user_state(user_id)
            if user_state.assigned_preset == "__custom__":
                await callback.answer("Already using custom")
            elif not user_state.custom_channels:
                await callback.answer(f"⚠️ {user_id} has no custom channels", show_alert=True)
            else:
                BotContext.data.set_user_mode_custom(user_id)
                await do_restart_if_running(user_id, callback.from_user.id)
                await callback.answer(f"Set {user_id} to custom")
        
        # Refresh
        users = {}
        for uid in BotContext.config.twitch_users:
            user_state = BotContext.data.get_user_state(uid)
            users[uid] = user_state.assigned_preset == "__custom__"
        await callback.message.edit_text(
            "👥 **Assign 'Custom Channels' to users**\n\nTap a user to toggle:",
            reply_markup=preset_users_keyboard(preset_name, users),
            parse_mode="Markdown",
        )
        return

    # Regular preset
    if user_id == "__all__":
        for uid in BotContext.config.twitch_users:
            BotContext.data.assign_preset_to_user(uid, preset_name)
            await do_restart_if_running(uid, callback.from_user.id)
        await callback.answer("✅ Assigned to all users", show_alert=True)
    else:
        user_state = BotContext.data.get_user_state(user_id)
        if user_state.assigned_preset == preset_name:
            BotContext.data.set_user_mode_default(user_id)
            await do_restart_if_running(user_id, callback.from_user.id)
            await callback.answer(f"Removed from {user_id}")
        else:
            BotContext.data.assign_preset_to_user(user_id, preset_name)
            await do_restart_if_running(user_id, callback.from_user.id)
            await callback.answer(f"Assigned to {user_id}")

    # Refresh the view
    users = {}
    for uid in BotContext.config.twitch_users:
        user_state = BotContext.data.get_user_state(uid)
        users[uid] = user_state.assigned_preset == preset_name

    await callback.message.edit_text(
        f"👥 **Assign '{escape_md(preset_name)}' to users**\n\nTap a user to toggle assignment:",
        reply_markup=preset_users_keyboard(preset_name, users),
        parse_mode="Markdown",
    )


# User preset assignment
@router.callback_query(F.data.startswith("user_preset:"))
async def user_assign_preset_menu(callback: CallbackQuery):
    """Show preset assignment menu for user."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    user_id = callback.data.split(":")[1]
    presets = BotContext.data.get_presets()

    await callback.message.edit_text(
        f"📋 **Assign preset to: {escape_md(user_id)}**\n\nSelect a preset:",
        reply_markup=assign_preset_keyboard(user_id, presets),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("assign_preset:"))
async def assign_preset_to_user(callback: CallbackQuery):
    """Assign preset to user."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    user_id = parts[1]
    preset_name = parts[2]

    if preset_name == "__default__":
        BotContext.data.set_user_mode_default(user_id)
        await callback.answer("✅ Using default channels", show_alert=True)
    elif preset_name == "__custom__":
        if not BotContext.data.set_user_mode_custom(user_id):
            await callback.answer("⚠️ No custom channels set! Add them first.", show_alert=True)
            return
        await callback.answer("✅ Using custom channels", show_alert=True)
    else:
        BotContext.data.assign_preset_to_user(user_id, preset_name)
        await callback.answer(f"✅ Assigned preset '{preset_name}'", show_alert=True)

    # Restart if running
    await do_restart_if_running(user_id, callback.from_user.id)

    # Refresh user view
    is_running = BotContext.process_manager.is_running(user_id)
    user_state = BotContext.data.get_user_state(user_id)

    status_emoji = "🟢 Running" if is_running else "🔴 Stopped"
    source_line = get_user_source_line(user_state)

    text = (
        f"👤 **User: {escape_md(user_id)}**\n\n"
        f"Status: {status_emoji}\n"
        f"{source_line}\n"
    )
    await callback.message.edit_text(
        text,
        reply_markup=user_control_keyboard(user_id, is_running),
        parse_mode="Markdown",
    )


# Custom channels for user
@router.callback_query(F.data.startswith("user_channels:"))
async def user_custom_channels_start(callback: CallbackQuery, state: FSMContext):
    """Start custom channels input for user."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    user_id = callback.data.split(":")[1]
    await state.set_state(UserStates.waiting_custom_channels)
    await state.update_data(user_id=user_id)

    await callback.message.edit_text(
        f"✏️ **Custom channels for: {escape_md(user_id)}**\n\n"
        "Enter channels (one per line or comma-separated):",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(StateFilter(UserStates.waiting_custom_channels))
async def user_custom_channels_input(message: Message, state: FSMContext):
    """Handle custom channels input."""
    if not is_whitelisted(message.from_user.id):
        return

    data = await state.get_data()
    user_id = data.get("user_id")

    channels = [ch.strip() for ch in message.text.replace(",", "\n").split("\n") if ch.strip()]
    
    if not channels:
        await message.answer("Please enter at least one channel.")
        return

    BotContext.data.set_user_custom_channels(user_id, channels)
    await state.clear()

    # Restart if running
    await do_restart_if_running(user_id, message.from_user.id)

    is_running = BotContext.process_manager.is_running(user_id)
    await message.answer(
        f"✅ Set {len(channels)} custom channel(s) for {escape_md(user_id)}",
        reply_markup=user_control_keyboard(user_id, is_running),
    )


# Cancel input
@router.callback_query(F.data == "cancel_input")
async def cancel_input(callback: CallbackQuery, state: FSMContext):
    """Cancel current input operation."""
    if not is_whitelisted(callback.from_user.id):
        await callback.answer("⛔ Not authorized", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()

    # Check if we were editing a preset
    if data.get("edit_mode") and data.get("preset_name"):
        preset_name = data["preset_name"]
        channels = data.get("channels", [])
        
        # Save any channels that were added
        if channels:
            for ch in channels:
                BotContext.data.add_channel_to_preset(preset_name, ch)
            await restart_users_with_preset(preset_name, callback.from_user.id)
        
        preset = BotContext.data.get_preset(preset_name)
        if preset:
            channels_text = "\n".join(f"  • {ch}" for ch in preset.channels) or "  (no channels)"
            await callback.message.edit_text(
                f"📋 **Preset: {preset.name}**\n\n"
                f"Channels:\n{channels_text}",
                reply_markup=preset_control_keyboard(preset_name),
                parse_mode="Markdown",
            )
            await callback.answer("Saved & restarted" if channels else "Cancelled")
            return

    # Default: go to main menu
    status, states = get_users_status()
    running = sum(1 for v in status.values() if v)
    total = len(status)

    text = (
        "🎮 **Twitch Farm Controller**\n\n"
        f"📊 Status: {running}/{total} instances running\n\n"
        "Choose an option:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    await callback.answer("Cancelled")

