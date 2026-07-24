import inspect
import logging

from colorama import Fore
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin

from controller.miner_runner import _snapshot_streamers, selected_ordered_channels
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
        priority=[Priority.ORDER],
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
    inspect.signature(Twitch.send_minute_watched_events).bind(
        Twitch,
        [],
        [Priority.ORDER],
        3,
    )


def test_order_priority_contract_and_telemetry_selection():
    source = inspect.getsource(Twitch.send_minute_watched_events)
    assert "max_watch_amount = 2" in source
    assert "streamers_index[:remaining_watch_amount()]" in source

    first = Streamer("first")
    second = Streamer("second")
    third = Streamer("third")
    fourth = Streamer("fourth")
    first.is_online = True
    first.online_at = 980
    second.is_online = False
    third.is_online = True
    third.online_at = 0
    fourth.is_online = True
    fourth.online_at = 900

    assert selected_ordered_channels(
        [first, second, third, fourth],
        now=1000,
    ) == ["third", "fourth"]

    first.online_at = 900
    assert selected_ordered_channels(
        [first, second, third, fourth],
        now=1000,
    ) == ["first", "third"]

    snapshot = _snapshot_streamers([first, third, fourth])
    first.is_online = False
    third.is_online = False
    assert selected_ordered_channels(snapshot, now=1000) == ["first", "third"]
