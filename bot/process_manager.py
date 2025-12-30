"""Process manager for Twitch miner instances."""

import json
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Callable, Awaitable

from bot.config import AppConfig, TwitchUserConfig
from bot.data import DataManager


class ProcessManager:
    """Manages Twitch miner subprocess instances."""

    def __init__(
        self,
        config: AppConfig,
        data_manager: DataManager,
        on_instance_failed: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.data = data_manager
        self.on_instance_failed = on_instance_failed
        self._processes: dict[str, subprocess.Popen] = {}
        self._runner_path = Path(__file__).parent / "miner_runner.py"

    def _build_miner_config(self, user_id: str, user_config: TwitchUserConfig) -> dict:
        """Build configuration dict for miner subprocess."""
        channels = self.data.get_user_channels(user_id, self.config.default_channels)
        
        return {
            "username": user_config.username,
            "password": user_config.password,
            "channels": channels,
        }

    def start_instance(self, user_id: str) -> bool:
        """Start a miner instance for a user."""
        if user_id in self._processes:
            proc = self._processes[user_id]
            if proc.poll() is None:  # Still running
                return False

        user_config = self.config.twitch_users.get(user_id)
        if not user_config:
            return False

        miner_config = self._build_miner_config(user_id, user_config)
        config_json = json.dumps(miner_config)

        try:
            proc = subprocess.Popen(
                [sys.executable, str(self._runner_path), config_json],
                cwd=Path(__file__).parent.parent,
            )
            self._processes[user_id] = proc
            self.data.set_user_running(user_id, True, proc.pid)
            return True
        except Exception as e:
            print(f"Failed to start instance for {user_id}: {e}")
            return False

    def stop_instance(self, user_id: str) -> bool:
        """Stop a miner instance for a user."""
        if user_id not in self._processes:
            self.data.set_user_running(user_id, False)
            return False

        proc = self._processes[user_id]
        if proc.poll() is None:  # Still running
            # Kill immediately - the miner library doesn't respond to SIGTERM quickly
            proc.kill()
            proc.wait()

        del self._processes[user_id]
        self.data.set_user_running(user_id, False)
        return True

    def restart_instance(self, user_id: str) -> bool:
        """Restart a miner instance for a user."""
        self.stop_instance(user_id)
        return self.start_instance(user_id)

    def is_running(self, user_id: str) -> bool:
        """Check if a miner instance is running for a user."""
        if user_id not in self._processes:
            return False
        return self._processes[user_id].poll() is None

    def get_running_users(self) -> list[str]:
        """Get list of users with running instances."""
        return [
            user_id for user_id in self._processes
            if self._processes[user_id].poll() is None
        ]

    def get_all_status(self) -> dict[str, bool]:
        """Get running status for all configured users."""
        return {
            user_id: self.is_running(user_id)
            for user_id in self.config.twitch_users
        }

    async def check_health(self) -> list[str]:
        """
        Check health of all instances.
        Returns list of user_ids that were found dead and restarted.
        """
        failed_users = []
        
        for user_id in list(self._processes.keys()):
            proc = self._processes[user_id]
            if proc.poll() is not None:  # Process has exited
                failed_users.append(user_id)
                
                # Notify callback if set
                if self.on_instance_failed:
                    await self.on_instance_failed(user_id)
                
                # Restart the instance
                self.start_instance(user_id)
        
        return failed_users

    def start_all(self) -> dict[str, bool]:
        """Start instances for all configured users."""
        results = {}
        for user_id in self.config.twitch_users:
            results[user_id] = self.start_instance(user_id)
        return results

    def stop_all(self) -> None:
        """Stop all running instances."""
        for user_id in list(self._processes.keys()):
            self.stop_instance(user_id)

    def cleanup(self) -> None:
        """Clean up all processes on shutdown."""
        self.stop_all()

