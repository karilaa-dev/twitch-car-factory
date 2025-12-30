import logging
from colorama import Fore
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.logger import LoggerSettings, ColorPalette
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings

twitch_miner = TwitchChannelPointsMiner(
    username="Ch3l0v3k",
    password="K7#mP9$vL2@nQ8xR4.",
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

twitch_miner.mine(
    [
        "ariesmarion", "popogich", "zhenyalarkin", "link1107"
    ],
    followers=False
)
