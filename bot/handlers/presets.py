"""Preset management handlers."""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from bot.keyboards import (
    presets_list_keyboard,
    preset_control_keyboard,
    system_preset_keyboard,
    preset_channels_keyboard,
    preset_users_keyboard,
    confirm_keyboard,
    cancel_keyboard,
    add_channels_keyboard,
)
from bot.handlers.context import (
    BotContext,
    PresetStates,
    PRESET_DEFAULT,
    PRESET_CUSTOM,
    USER_ALL,
    require_whitelist,
    is_whitelisted,
    escape_md,
    restart_if_running,
    restart_users_with_preset,
    refresh_presets_list,
)


router = Router()


# Preset details view

@router.callback_query(F.data.startswith("preset:"))
@require_whitelist
async def show_preset(callback: CallbackQuery):
    """Show preset details."""
    preset_name = callback.data.split(":", 1)[1]
    
    if preset_name == PRESET_DEFAULT:
        channels = BotContext.config.default_channels
        channels_text = "\n".join(f"  • {ch}" for ch in channels) or "  (no channels)"
        await callback.message.edit_text(
            f"🔄 **Default Channels**\n\nChannels:\n{channels_text}",
            reply_markup=system_preset_keyboard(PRESET_DEFAULT),
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    
    if preset_name == PRESET_CUSTOM:
        await callback.message.edit_text(
            "✏️ **Custom Channels**\n\n"
            "Each user has their own custom channels.\n"
            "Assign this to users who have custom channels set.",
            reply_markup=system_preset_keyboard(PRESET_CUSTOM),
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    
    preset = BotContext.data.get_preset(preset_name)
    if not preset:
        await callback.answer("Preset not found", show_alert=True)
        return

    channels_text = "\n".join(f"  • {ch}" for ch in preset.channels) or "  (no channels)"
    await callback.message.edit_text(
        f"📋 **Preset: {preset.name}**\n\nChannels:\n{channels_text}",
        reply_markup=preset_control_keyboard(preset_name),
        parse_mode="Markdown",
    )
    await callback.answer()


# Preset creation

@router.callback_query(F.data == "preset_action:create")
@require_whitelist
async def preset_create_start(callback: CallbackQuery, state: FSMContext):
    """Start preset creation flow."""
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
    edit_mode = data.get("edit_mode", False)
    text = message.text.strip().lower()

    if text == "done":
        await _finish_adding_channels(message, state, preset_name, channels, edit_mode)
        return

    # Parse and add channels
    new_channels = [ch.strip() for ch in text.replace(",", "\n").split("\n") if ch.strip()]
    channels.extend(new_channels)
    await state.update_data(channels=channels)
    
    await message.answer(
        f"Added {len(new_channels)} channel(s). Total: {len(channels)}\n"
        "Add more or press Done to finish.",
        reply_markup=add_channels_keyboard(),
    )


async def _finish_adding_channels(event: Message | CallbackQuery, state: FSMContext, 
                                   preset_name: str, channels: list[str], edit_mode: bool):
    """Complete the channel adding flow."""
    if not channels:
        msg = "Please add at least one channel before finishing."
        if isinstance(event, CallbackQuery):
            await event.answer(msg, show_alert=True)
        else:
            await event.answer(msg)
        return

    telegram_id = event.from_user.id
    
    if edit_mode:
        for ch in channels:
            BotContext.data.add_channel_to_preset(preset_name, ch)
        restarted = await restart_users_with_preset(preset_name, telegram_id)
        restart_msg = f" Restarted {restarted} user(s)." if restarted else ""
        await state.clear()
        
        preset = BotContext.data.get_preset(preset_name)
        channels_text = "\n".join(f"  • {ch}" for ch in preset.channels) or "  (no channels)"
        response_text = f"✅ Added {len(channels)} channel(s) to '{preset_name}'!{restart_msg}\n\nChannels:\n{channels_text}"
        keyboard = preset_control_keyboard(preset_name)
    else:
        BotContext.data.create_preset(preset_name, channels)
        await state.clear()
        response_text = f"✅ Preset '{preset_name}' created with {len(channels)} channels!"
        keyboard = presets_list_keyboard(BotContext.data.get_presets())

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(response_text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(response_text, reply_markup=keyboard)


@router.callback_query(F.data == "done_adding_channels", StateFilter(PresetStates.waiting_channel_name))
@require_whitelist
async def done_adding_channels(callback: CallbackQuery, state: FSMContext):
    """Handle Done button when adding channels."""
    data = await state.get_data()
    await _finish_adding_channels(
        callback, state,
        data.get("preset_name"),
        data.get("channels", []),
        data.get("edit_mode", False),
    )


# Preset editing

@router.callback_query(F.data.startswith("preset_edit:"))
@require_whitelist
async def preset_edit(callback: CallbackQuery, state: FSMContext):
    """Handle preset edit actions."""
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
@require_whitelist
async def preset_remove_channel(callback: CallbackQuery):
    """Remove a channel from preset."""
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
        await refresh_presets_list(callback)


@router.callback_query(F.data.startswith("confirm:"))
@require_whitelist
async def handle_confirm(callback: CallbackQuery):
    """Handle confirmation callbacks."""
    parts = callback.data.split(":", 2)
    action = parts[1]
    context = parts[2]

    if action == "delete_preset":
        BotContext.data.delete_preset(context)
        await callback.answer(f"✅ Preset '{context}' deleted", show_alert=True)
        await refresh_presets_list(callback)


# Preset assignment to users (from preset side)

@router.callback_query(F.data.startswith("preset_assign:"))
@require_whitelist
async def preset_assign_menu(callback: CallbackQuery):
    """Show user selection for preset assignment."""
    preset_name = callback.data.split(":", 1)[1]
    
    # Build user -> is_assigned mapping
    users = {}
    for user_id in BotContext.config.twitch_users:
        user_state = BotContext.data.get_user_state(user_id)
        users[user_id] = user_state.assigned_preset == preset_name

    # Determine title based on preset type
    if preset_name == PRESET_DEFAULT:
        title = "Assign 'Default Channels' to users"
    elif preset_name == PRESET_CUSTOM:
        title = "Assign 'Custom Channels' to users"
    else:
        preset = BotContext.data.get_preset(preset_name)
        if not preset:
            await callback.answer("Preset not found", show_alert=True)
            return
        title = f"Assign '{escape_md(preset_name)}' to users"

    await callback.message.edit_text(
        f"👥 **{title}**\n\nTap a user to toggle:",
        reply_markup=preset_users_keyboard(preset_name, users),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_assign_user:"))
@require_whitelist
async def preset_assign_user(callback: CallbackQuery):
    """Toggle preset assignment for a user."""
    parts = callback.data.split(":", 2)
    preset_name = parts[1]
    user_id = parts[2]
    telegram_id = callback.from_user.id

    if preset_name == PRESET_DEFAULT:
        await _assign_default_preset(callback, user_id, telegram_id)
    elif preset_name == PRESET_CUSTOM:
        await _assign_custom_preset(callback, user_id, telegram_id)
    else:
        await _assign_regular_preset(callback, preset_name, user_id, telegram_id)


async def _assign_default_preset(callback: CallbackQuery, user_id: str, telegram_id: int):
    """Handle default preset assignment."""
    if user_id == USER_ALL:
        for uid in BotContext.config.twitch_users:
            BotContext.data.set_user_mode_default(uid)
            await restart_if_running(uid, telegram_id)
        await callback.answer("✅ All users set to default", show_alert=True)
    else:
        user_state = BotContext.data.get_user_state(user_id)
        if user_state.assigned_preset == PRESET_DEFAULT:
            await callback.answer("Already using default")
        else:
            BotContext.data.set_user_mode_default(user_id)
            await restart_if_running(user_id, telegram_id)
            await callback.answer(f"Set {user_id} to default")

    await _refresh_preset_users_view(callback, PRESET_DEFAULT)


async def _assign_custom_preset(callback: CallbackQuery, user_id: str, telegram_id: int):
    """Handle custom preset assignment."""
    if user_id == USER_ALL:
        count = 0
        for uid in BotContext.config.twitch_users:
            if BotContext.data.set_user_mode_custom(uid):
                count += 1
                await restart_if_running(uid, telegram_id)
        await callback.answer(f"✅ Set {count} users to custom", show_alert=True)
    else:
        user_state = BotContext.data.get_user_state(user_id)
        if user_state.assigned_preset == PRESET_CUSTOM:
            await callback.answer("Already using custom")
        elif not user_state.custom_channels:
            await callback.answer(f"⚠️ {user_id} has no custom channels", show_alert=True)
        else:
            BotContext.data.set_user_mode_custom(user_id)
            await restart_if_running(user_id, telegram_id)
            await callback.answer(f"Set {user_id} to custom")

    await _refresh_preset_users_view(callback, PRESET_CUSTOM)


async def _assign_regular_preset(callback: CallbackQuery, preset_name: str, user_id: str, telegram_id: int):
    """Handle regular preset assignment."""
    if user_id == USER_ALL:
        for uid in BotContext.config.twitch_users:
            BotContext.data.assign_preset_to_user(uid, preset_name)
            await restart_if_running(uid, telegram_id)
        await callback.answer("✅ Assigned to all users", show_alert=True)
    else:
        user_state = BotContext.data.get_user_state(user_id)
        if user_state.assigned_preset == preset_name:
            # Toggle off - switch to default
            BotContext.data.set_user_mode_default(user_id)
            await restart_if_running(user_id, telegram_id)
            await callback.answer(f"Removed from {user_id}")
        else:
            BotContext.data.assign_preset_to_user(user_id, preset_name)
            await restart_if_running(user_id, telegram_id)
            await callback.answer(f"Assigned to {user_id}")

    await _refresh_preset_users_view(callback, preset_name)


async def _refresh_preset_users_view(callback: CallbackQuery, preset_name: str):
    """Refresh the preset users assignment view."""
    users = {}
    for uid in BotContext.config.twitch_users:
        user_state = BotContext.data.get_user_state(uid)
        users[uid] = user_state.assigned_preset == preset_name

    if preset_name == PRESET_DEFAULT:
        title = "Assign 'Default Channels' to users"
    elif preset_name == PRESET_CUSTOM:
        title = "Assign 'Custom Channels' to users"
    else:
        title = f"Assign '{escape_md(preset_name)}' to users"

    await callback.message.edit_text(
        f"👥 **{title}**\n\nTap a user to toggle:",
        reply_markup=preset_users_keyboard(preset_name, users),
        parse_mode="Markdown",
    )

