# Twitch Farm Telegram Bot

A Telegram bot for controlling multiple Twitch Channel Points Miner instances.

## Features

- **Multi-user support**: Run separate miner instances for each Twitch account
- **Channel presets**: Create and manage channel list presets for easy switching
- **Instance control**: Start, stop, and restart individual or all instances
- **Health monitoring**: Automatic health checks every minute with notifications on failures
- **Auto-restart**: Crashed instances are automatically restarted
- **Whitelist**: Only authorized Telegram users can control the bot

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure the bot

Copy the example config and edit it:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:

1. Set your Telegram bot token (get one from [@BotFather](https://t.me/BotFather))
2. Add your Telegram user ID to the whitelist
3. Add your Twitch accounts under `twitch_users`

### 3. Run the bot

```bash
uv run python main.py
```

## Configuration

### config.yaml

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN_HERE"
  whitelist:
    - 123456789  # Your Telegram user ID

twitch_users:
  my_account:
    username: "my_twitch_username"
    password: "my_password"
    claim_drops: true
    claim_moments: true
    watch_streak: true

default_channels:
  - "channel1"
  - "channel2"
```

### Getting your Telegram user ID

Send `/start` to [@userinfobot](https://t.me/userinfobot) to get your Telegram user ID.

## Bot Commands

- `/start` - Open the control panel

### Control Panel Features

- **Users**: View and manage all Twitch users
  - Start/stop/restart individual instances
  - Assign channel presets
  - Set custom channels
- **Presets**: Manage channel presets
  - Create new presets
  - Add/remove channels
  - Delete presets
- **Start All / Stop All**: Control all instances at once

## Architecture

The bot runs each Twitch user as a separate subprocess, allowing independent control and isolation. The health monitor checks all instances every minute and:

1. Detects crashed instances
2. Sends notifications to all whitelisted users
3. Automatically restarts failed instances

## Files

- `config.yaml` - Main configuration (not tracked in git)
- `data/presets.json` - Channel presets
- `data/state.json` - User states and assignments
- `cookies/` - Twitch session cookies

