"""Telegram bot handlers package."""

from aiogram import Router

from bot.handlers.context import BotContext
from bot.handlers import menu, users, presets


# Create main router and include sub-routers
router = Router()
router.include_router(menu.router)
router.include_router(users.router)
router.include_router(presets.router)

__all__ = ["router", "BotContext"]

