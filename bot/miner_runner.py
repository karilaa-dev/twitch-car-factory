"""Script to run a Twitch Channel Points Miner instance."""

import sys
import json
import logging
from colorama import Fore
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.logger import LoggerSettings, ColorPalette
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings


def run_miner(config: dict) -> None:
    """Run the Twitch miner with the given configuration."""
    username = config["username"]
    password = config["password"]
    channels = config["channels"]

    twitch_miner = TwitchChannelPointsMiner(
        username=username,
        password=password,
        claim_drops_startup=False,
        priority=[
            Priority.DROPS,
            Priority.ORDER,
            Priority.STREAK,
        ],
        enable_analytics=False,
        disable_ssl_cert_verification=False,
        disable_at_in_nickname=False,
        logger_settings=LoggerSettings(
            save=False,
            console_level=logging.INFO,
            console_username=True,
            auto_clear=True,
            time_zone="",
            file_level=logging.WARNING,
            emoji=True,
            less=True,
            colored=True,
            color_palette=ColorPalette(
                STREAMER_online="GREEN",
                streamer_offline="red",
                BET_wiN=Fore.MAGENTA
            )
        ),
        streamer_settings=StreamerSettings(
            make_predictions=False,
            follow_raid=False,
            claim_drops=True,
            claim_moments=True,
            watch_streak=True,
            chat=ChatPresence.ONLINE,
        )
    )

    twitch_miner.mine(channels, followers=False)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python miner_runner.py '<json_config>'")
        sys.exit(1)
    
    config_json = sys.argv[1]
    config = json.loads(config_json)
    run_miner(config)

