# Telegram Bot-to-Bot Bridge

Solves Telegram's bot-to-bot limitation: **bots cannot see messages from other bots** in groups. This bridge uses a MTProto user account to relay messages between bots with proper mention entities.

## How It Works

```
Bot A tags @BotB in a group message
  ↓ INSTANT
Bridge sends a relay message (no mention — BotB ignores, humans see it forming)
  ↓ Bot A streams/edits its message
Bridge mirrors edits in real-time (throttled to avoid rate limits)
  ↓ Edits stop for 5 seconds
Bridge edits relay to add @BotB mention via formatting_entities
  ↓ BotB's Bot API receives the mention
BotB triggers and responds
  ↓ 2 seconds later
Bridge auto-deletes the relay message (clean group chat)
```

**No HTML parsing, no truncation, no content corruption.** Uses Telethon's `formatting_entities` for clean mention injection.

## Features

- **Multi-group** — watch multiple Telegram groups simultaneously
- **Multi-bot** — relay between any number of bots
- **Smart relay** — instant send, mirror streaming edits, mention only after silence
- **Auto-delete** — relay messages are deleted after the target bot receives them
- **Rate limit handling** — automatic FloodWait retry with backoff
- **Relay logging** — saves all relayed messages to `relay/history.jsonl`
- **systemd ready** — runs as a background service with auto-restart

## Requirements

- Python 3.8+
- [Telethon](https://github.com/LonamiWebs/Telethon)
- A Telegram user account (for the MTProto session)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org/apps)

## Quick Start

### 1. Install

```bash
git clone https://github.com/naorbrig/openclaw-telegram-bridge.git
cd openclaw-telegram-bridge
pip install telethon
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
# Get from https://my.telegram.org/apps
TG_API_ID=12345678
TG_API_HASH=abcdef1234567890abcdef1234567890

# Group channel IDs (MTProto format — without the -100 prefix)
# To find your group ID: add the bot, send a message, check bot API getUpdates
# If group ID is -1001234567890, use 1234567890
BRIDGE_GROUPS=1234567890,9876543210

# Bot configurations
BRIDGE_BOTS='[
  {"username": "my_first_bot", "alts": ["my_first_bot"], "mention": "@my_first_bot"},
  {"username": "my_second_bot", "alts": ["my_second_bot", "mysecondbot"], "mention": "@my_second_bot"}
]'

# Timing (seconds)
MENTION_SILENCE=5
DELETE_DELAY=2
EDIT_THROTTLE=1.5
```

### 3. Authenticate

```bash
python bridge.py
```

First run prompts for your phone number and verification code. The session is saved to a `.session` file and reused on subsequent runs.

### 4. Run

```bash
# Foreground
python bridge.py

# Background
nohup python bridge.py > bridge.log 2>&1 &

# Or as a systemd service (recommended)
sudo cp bridge.service.example /etc/systemd/system/telegram-bridge.service
# Edit the service file with your paths and user
sudo systemctl daemon-reload
sudo systemctl enable telegram-bridge
sudo systemctl start telegram-bridge
```

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TG_API_ID` | Yes | — | Telegram API ID from my.telegram.org |
| `TG_API_HASH` | Yes | — | Telegram API hash from my.telegram.org |
| `TG_SESSION_NAME` | No | `bridge_session` | Name for the session file |
| `BRIDGE_GROUPS` | Yes | — | Comma-separated MTProto channel IDs |
| `BRIDGE_BOTS` | Yes | — | JSON array of bot configurations |
| `MENTION_SILENCE` | No | `5` | Seconds of no edits before adding mention |
| `DELETE_DELAY` | No | `2` | Seconds after mention before deleting relay |
| `EDIT_THROTTLE` | No | `1.5` | Minimum seconds between mirror edits |

### Bot Configuration

Each bot in `BRIDGE_BOTS` has:

| Field | Required | Description |
|-------|----------|-------------|
| `username` | Yes | Bot's Telegram username (lowercase, no @) |
| `alts` | No | Alternative usernames to match (for typos/variations) |
| `mention` | Yes | The mention text to use (e.g., `@my_bot`) |

### Finding Group IDs

1. Add your bot to the group
2. Send a message in the group
3. Check `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find the `chat.id` — it will be negative (e.g., `-1001234567890`)
5. Remove the `-100` prefix for MTProto format: `1234567890`

## How the Mention System Works

Telegram's Bot API has a hard limitation: **bots cannot see messages from other bots**. This is server-side and cannot be bypassed with privacy settings, admin status, or any configuration.

This bridge works around it by:

1. **Reading** bot messages via MTProto (user account — can see everything)
2. **Relaying** the message as the user account with a proper `InputMessageEntityMentionName` entity
3. The relay message contains a **structural mention** (not just text `@bot`) that Telegram's server recognizes
4. The target bot's Bot API delivers the message because it came from a **user account** with a proper mention
5. The relay is **auto-deleted** after the target bot has time to process it

## Troubleshooting

### "FloodWait" errors
The bridge handles these automatically with retry. If you see many FloodWait errors, increase `EDIT_THROTTLE` to reduce edit frequency.

### Messages not being relayed
- Check that the bot username in `BRIDGE_BOTS` matches exactly (lowercase)
- Check that the group channel ID in `BRIDGE_GROUPS` is correct (MTProto format)
- Check that the user account is a member of the group
- Check `bridge.log` for `[NEW]` and `[MENTION]` entries

### Relay messages not being deleted
- The target bot may be trying to react to the relay before it's deleted — harmless
- Increase `DELETE_DELAY` if you see this frequently

### Bot not responding to relayed messages
- Ensure the bot's privacy mode is appropriate for the group
- The relay message must contain the bot's username for the bridge to trigger
- Check that `formatting_entities` are being added (look for `MENTION added` in logs)

## Architecture

```
┌─────────────────────────────────────────────┐
│                    HOST                      │
│                                              │
│  ┌─────────────────────────────────────┐    │
│  │  Telegram Bot-to-Bot Bridge         │    │
│  │  (MTProto user session)             │    │
│  │                                      │    │
│  │  Watches: Group 1, Group 2, ...      │    │
│  │  Relays:  Bot A ↔ Bot B ↔ Bot C     │    │
│  │  Method:  formatting_entities        │    │
│  └─────────────────────────────────────┘    │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Bot A   │  │  Bot B   │  │  Bot C   │  │
│  │ (Docker) │  │ (Docker) │  │ (Docker) │  │
│  └──────────┘  └──────────┘  └──────────┘  │
└─────────────────────────────────────────────┘
```

## Security

- The bridge runs on the **host**, not inside bot containers — bots cannot modify or stop it
- The MTProto session file (`.session`) contains your Telegram account access — **keep it secure**
- Bot tokens are NOT needed by the bridge — it uses the user account only
- Relay messages are auto-deleted to keep the group chat clean
- The bridge only watches configured groups — it cannot access other chats

## License

MIT
