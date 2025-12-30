"""Health monitoring for miner instances."""

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Bot

if TYPE_CHECKING:
    from bot.config import AppConfig
    from bot.process_manager import ProcessManager

logger = logging.getLogger(__name__)

MAX_RESTART_ATTEMPTS = 5


class HealthMonitor:
    """Monitors miner instances and sends notifications on failures."""

    def __init__(
        self,
        bot: Bot,
        config: "AppConfig",
        process_manager: "ProcessManager",
        check_interval: int = 60,
    ):
        self.bot = bot
        self.config = config
        self.process_manager = process_manager
        self.check_interval = check_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._restart_attempts: dict[str, int] = {}  # user_id -> attempt count
        self._failed_permanently: set[str] = set()  # users that failed after max attempts

    async def _notify(self, message: str) -> None:
        """Send notification to all whitelisted users."""
        for telegram_user_id in self.config.telegram.whitelist:
            try:
                await self.bot.send_message(
                    telegram_user_id,
                    message,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to send notification to {telegram_user_id}: {e}")

    async def _handle_failed_instance(self, user_id: str) -> None:
        """Handle a failed instance with retry logic."""
        # Skip if already permanently failed
        if user_id in self._failed_permanently:
            return

        # Increment attempt counter
        self._restart_attempts[user_id] = self._restart_attempts.get(user_id, 0) + 1
        attempt = self._restart_attempts[user_id]

        if attempt == 1:
            # First failure - notify and restart
            await self._notify(
                f"⚠️ **Instance Down**\n\n"
                f"User: `{user_id}`\n"
                f"Attempting to restart..."
            )

        logger.warning(f"Instance for {user_id} failed (attempt {attempt}/{MAX_RESTART_ATTEMPTS})")

        # Try to restart
        success = self.process_manager.start_instance(user_id)

        if success:
            # Reset counter on successful restart
            self._restart_attempts[user_id] = 0
            if attempt > 1:
                await self._notify(
                    f"✅ **Instance Recovered**\n\n"
                    f"User: `{user_id}`\n"
                    f"Successfully restarted after {attempt} attempt(s)."
                )
        elif attempt >= MAX_RESTART_ATTEMPTS:
            # Max attempts reached
            self._failed_permanently.add(user_id)
            await self._notify(
                f"❌ **Instance Failed Permanently**\n\n"
                f"User: `{user_id}`\n"
                f"Failed to restart after {MAX_RESTART_ATTEMPTS} attempts.\n"
                f"Manual intervention required."
            )
            logger.error(f"Instance for {user_id} failed permanently after {MAX_RESTART_ATTEMPTS} attempts")

    async def _check_loop(self) -> None:
        """Main health check loop."""
        while self._running:
            try:
                # Check all instances that should be running
                for user_id in list(self.process_manager._processes.keys()):
                    if not self.process_manager.is_running(user_id):
                        await self._handle_failed_instance(user_id)
                        
            except Exception as e:
                logger.error(f"Health check error: {e}")
            
            await asyncio.sleep(self.check_interval)

    def start(self) -> None:
        """Start the health monitor."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Health monitor started")

    async def stop(self) -> None:
        """Stop the health monitor."""
        if not self._running:
            return
        
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    def reset_user(self, user_id: str) -> None:
        """Reset failure state for a user (call when manually starting)."""
        self._restart_attempts.pop(user_id, None)
        self._failed_permanently.discard(user_id)
