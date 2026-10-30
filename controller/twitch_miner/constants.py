"""Twitch protocol constants, maintained independently of a third-party miner.

Persisted operations and playlist protocol adapted from DevilXD/TwitchDropsMiner
at 99d12fd2d815d7737258ed57405c63c0686bd3e1 (MIT, copyright 2024 DevilXD).
See THIRD_PARTY_NOTICES.md for the upstream license.
"""

from copy import deepcopy


WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TV_CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
ANDROID_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
GQL_URL = "https://gql.twitch.tv/gql"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
ANDROID_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 16; SM-S911B Build/TP1A.220624.014) "
    "tv.twitch.android.app/31.8.0/3108006"
)
TV_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 7.1; Smart Box C1) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# Keep hashes together so a Twitch protocol change does not affect the engine.
OPERATIONS = {
    "GetStreamInfo": (
        "VideoPlayerStreamInfoOverlayChannel",
        "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
        {},
    ),
    "GetIDFromLogin": (
        "GetIDFromLogin",
        "94e82a7b1e3c21e186daa73ee2afc4b8f23bade1fbbff6fe8ac133f50a2f58ca",
        {},
    ),
    "ClaimCommunityPoints": (
        "ClaimCommunityPoints",
        "46aaeebe02c99afdf4fc97c7c0cba964124bf6b0af229395f1f6d1feed05b3d0",
        {},
    ),
    "ClaimDrop": (
        "DropsPage_ClaimDropRewards",
        "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
        {},
    ),
    "ChannelPointsContext": (
        "ChannelPointsContext",
        "374314de591e69925fce3ddc2bcf085796f56ebb8cad67a0daa3165c03adc345",
        {},
    ),
    "Inventory": (
        "Inventory",
        "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
        {"fetchRewardCampaigns": False},
    ),
    "CurrentDrop": (
        "DropCurrentSessionContext",
        "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        {"channelLogin": ""},
    ),
    "Campaigns": (
        "ViewerDropsDashboard",
        "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb",
        {"fetchRewardCampaigns": False},
    ),
    "CampaignDetails": (
        "DropCampaignDetails",
        "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1",
        {},
    ),
    "AvailableDrops": (
        "DropsHighlightService_AvailableDrops",
        "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
        {},
    ),
    "PlaybackAccessToken": (
        "PlaybackAccessToken",
        "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
        {"isLive": True, "isVod": False, "platform": "web", "playerType": "site", "vodID": ""},
    ),
}


def operation_payload(name: str, variables: dict | None = None) -> dict:
    operation, digest, defaults = OPERATIONS[name]
    return {
        "operationName": operation,
        "variables": {**deepcopy(defaults), **deepcopy(variables or {})},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": digest}},
    }
