"""Main menu and navigation handlers."""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.keyboards import main_menu_keyboard, users_list_keyboard, presets_list_keyboard
from bot.handlers.context import (
    BotContext,
    require_whitelist,
    is_whitelisted,
    get_users_status,
    build_main_menu_text,
    refresh_main_menu,
    refresh_presets_list,
)


router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    """Handle /start command."""
    if not is_whitelisted(message.from_user.id):
        await message.answer("⛔ You are not authorized to use this bot.")
        return

    await message.answer(
        build_main_menu_text(),
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "menu:main")
@require_whitelist
async def menu_main(callback: CallbackQuery, state: FSMContext):
    """Show main menu."""
    await state.clear()
    await refresh_main_menu(callback)
    await callback.answer()


@router.callback_query(F.data == "menu:users")
@require_whitelist
async def menu_users(callback: CallbackQuery):
    """Show users list."""
    status, states = get_users_status()
    await callback.message.edit_text(
        "👥 **Twitch Users**\n\n🟢 = running, 🔴 = stopped\n\nSelect a user to manage:",
        reply_markup=users_list_keyboard(status, states),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:presets")
@require_whitelist
async def menu_presets(callback: CallbackQuery, state: FSMContext):
    """Show presets list."""
    await state.clear()
    await refresh_presets_list(callback)
    await callback.answer()


@router.callback_query(F.data == "cancel_input")
@require_whitelist
async def cancel_input(callback: CallbackQuery, state: FSMContext):
    """Cancel current input operation and return to appropriate view."""
    from bot.keyboards import preset_control_keyboard
    from bot.handlers.context import restart_users_with_preset
    
    data = await state.get_data()
    await state.clear()

    # If editing a preset, save any added channels and return to preset view
    if data.get("edit_mode") and data.get("preset_name"):
        preset_name = data["preset_name"]
        channels = data.get("channels", [])
        
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
    await refresh_main_menu(callback)
    await callback.answer("Cancelled")

