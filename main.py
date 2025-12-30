"""Main entry point for Twitch Farm Telegram Bot."""

import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import AppConfig
from bot.data import DataManager
from bot.process_manager import ProcessManager
from bot.handlers import router, BotContext
from bot.health_monitor import HealthMonitor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Reduce aiogram logging
logging.getLogger("aiogram").setLevel(logging.WARNING)


async def main() -> None:
    """Main application entry point."""
    # Load configuration
    config = AppConfig.load()
    
    if config.telegram.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("Please configure your bot token in config.yaml")
        sys.exit(1)

    if not config.telegram.whitelist:
        logger.warning("No users in whitelist - nobody will be able to use the bot!")

    if not config.twitch_users:
        logger.warning("No Twitch users configured - add users in config.yaml")

    # Initialize data manager
    data_manager = DataManager()
    data_manager.initialize_users(list(config.twitch_users.keys()))

    # Initialize process manager
    process_manager = ProcessManager(config, data_manager)

    # Initialize bot
    bot = Bot(
        token=config.telegram.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )

    # Initialize health monitor
    health_monitor = HealthMonitor(bot, config, process_manager)

    # Set up context for handlers
    BotContext.config = config
    BotContext.data = data_manager
    BotContext.process_manager = process_manager
    BotContext.health_monitor = health_monitor
    BotContext.bot = bot

    # Initialize dispatcher
    dp = Dispatcher()
    dp.include_router(router)

    # Shutdown handler
    async def shutdown():
        logger.info("Shutting down...")
        await health_monitor.stop()
        process_manager.cleanup()
        await bot.session.close()

    # Handle signals
    loop = asyncio.get_event_loop()
    
    def signal_handler(sig):
        logger.info(f"Received signal {sig}")
        loop.create_task(shutdown())
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))

    try:
        # Start health monitor
        health_monitor.start()
        
        # Log startup info
        logger.info(f"Starting Twitch Farm Bot")
        logger.info(f"Configured users: {list(config.twitch_users.keys())}")
        logger.info(f"Whitelisted Telegram IDs: {config.telegram.whitelist}")

        # Auto-start instances if configured
        if config.settings.autostart_instances:
            logger.info("Auto-starting all instances...")
            for user_id in config.twitch_users.keys():
                if process_manager.start_instance(user_id):
                    logger.info(f"Started instance for {user_id}")
                else:
                    logger.error(f"Failed to start instance for {user_id}")

        # Start polling
        await dp.start_polling(bot)
        
    except asyncio.CancelledError:
        pass
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
