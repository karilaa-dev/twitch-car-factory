"""User management handlers."""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from bot.keyboards import (
    user_control_keyboard,
    assign_preset_keyboard,
    cancel_keyboard,
)
from bot.handlers.context import (
    BotContext,
    UserStates,
    PRESET_DEFAULT,
    PRESET_CUSTOM,
    require_whitelist,
    is_whitelisted,
    escape_md,
    restart_instance,
    restart_if_running,
    refresh_main_menu,
    refresh_user_view,
    get_user_source_line,
)


router = Router()


# User control panel

@router.callback_query(F.data.startswith("user:"))
@require_whitelist
async def show_user(callback: CallbackQuery):
    """Show user control panel."""
    user_id = callback.data.split(":")[1]
    await refresh_user_view(callback, user_id)
    await callback.answer()


# User actions (start/stop/restart)

@router.callback_query(F.data.startswith("user_action:"))
@require_whitelist
async def user_action(callback: CallbackQuery):
    """Handle user start/stop/restart actions."""
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
        await restart_instance(user_id, callback.from_user.id)
    
    else:
        await callback.answer("Unknown action", show_alert=True)
        return

    await refresh_user_view(callback, user_id)


# Global start/stop all

@router.callback_query(F.data == "action:start_all")
@require_whitelist
async def action_start_all(callback: CallbackQuery):
    """Start all instances."""
    results = BotContext.process_manager.start_all()
    started = sum(1 for v in results.values() if v)
    
    if BotContext.health_monitor:
        for user_id, success in results.items():
            if success:
                BotContext.health_monitor.reset_user(user_id)
    
    await callback.answer(f"✅ Started {started}/{len(results)} instances", show_alert=True)
    await refresh_main_menu(callback)


@router.callback_query(F.data == "action:stop_all")
@require_whitelist
async def action_stop_all(callback: CallbackQuery):
    """Stop all instances."""
    BotContext.process_manager.stop_all()
    await callback.answer("✅ Stopped all instances", show_alert=True)
    await refresh_main_menu(callback)


# Preset assignment from user view

@router.callback_query(F.data.startswith("user_preset:"))
@require_whitelist
async def user_assign_preset_menu(callback: CallbackQuery):
    """Show preset assignment menu for user."""
    user_id = callback.data.split(":")[1]
    presets = BotContext.data.get_presets()

    await callback.message.edit_text(
        f"📋 **Assign preset to: {escape_md(user_id)}**\n\nSelect a preset:",
        reply_markup=assign_preset_keyboard(user_id, presets),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("assign_preset:"))
@require_whitelist
async def assign_preset_to_user(callback: CallbackQuery):
    """Assign preset to user."""
    parts = callback.data.split(":", 2)
    user_id = parts[1]
    preset_name = parts[2]

    if preset_name == PRESET_DEFAULT:
        BotContext.data.set_user_mode_default(user_id)
        await callback.answer("✅ Using default channels", show_alert=True)
    elif preset_name == PRESET_CUSTOM:
        if not BotContext.data.set_user_mode_custom(user_id):
            await callback.answer("⚠️ No custom channels set! Add them first.", show_alert=True)
            return
        await callback.answer("✅ Using custom channels", show_alert=True)
    else:
        BotContext.data.assign_preset_to_user(user_id, preset_name)
        await callback.answer(f"✅ Assigned preset '{preset_name}'", show_alert=True)

    await restart_if_running(user_id, callback.from_user.id)
    await refresh_user_view(callback, user_id)


# Custom channels

@router.callback_query(F.data.startswith("user_channels:"))
@require_whitelist
async def user_custom_channels_start(callback: CallbackQuery, state: FSMContext):
    """Start custom channels input for user."""
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

    await restart_if_running(user_id, message.from_user.id)

    is_running = BotContext.process_manager.is_running(user_id)
    await message.answer(
        f"✅ Set {len(channels)} custom channel(s) for {escape_md(user_id)}",
        reply_markup=user_control_keyboard(user_id, is_running),
    )

