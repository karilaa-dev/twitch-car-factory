import inspect
import logging

from colorama import Fore
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import StreamerSettings
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings


def test_upstream_miner_supports_runner_configuration():
    """Keep dependency upgrades compatible with controller.miner_runner."""

    logger_settings = LoggerSettings(
        save=False,
        console_level=logging.INFO,
        console_username=True,
        auto_clear=False,
        time_zone="",
        file_level=logging.WARNING,
        emoji=True,
        less=False,
        colored=False,
        color_palette=ColorPalette(
            STREAMER_online="GREEN",
            streamer_offline="red",
            BET_wiN=Fore.MAGENTA,
        ),
    )
    streamer_settings = StreamerSettings(
        make_predictions=False,
        follow_raid=False,
        claim_drops=True,
        claim_moments=True,
        watch_streak=True,
        chat=ChatPresence.ONLINE,
    )

    inspect.signature(TwitchChannelPointsMiner).bind(
        username="dependency-contract",
        password=None,
        claim_drops_startup=False,
        priority=[Priority.DROPS, Priority.ORDER, Priority.STREAK],
        enable_analytics=False,
        disable_ssl_cert_verification=False,
        disable_at_in_nickname=False,
        logger_settings=logger_settings,
        streamer_settings=streamer_settings,
    )
    oauth_request = TwitchLogin.send_oauth_request
    assert callable(oauth_request)
    inspect.signature(oauth_request).bind(
        TwitchLogin,
        "https://id.twitch.tv/oauth2/device",
        {},
    )
