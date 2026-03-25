#!/usr/bin/env python3
"""
Telegram Bot-to-Bot Bridge — Smart Instant Relay

Solves Telegram's bot-to-bot limitation: bots cannot see messages from other bots.
This bridge uses a MTProto user account to relay messages between bots using
formatting_entities mentions, then auto-deletes the relay message.

How it works:
1. Bot A tags @BotB in a message
2. Bridge instantly sends a relay (no mention — BotB ignores it, humans see it forming)
3. Bridge mirrors streaming edits in real-time (throttled)
4. After edits stop (silence period), bridge edits the relay to add a proper mention entity
5. BotB's Bot API receives the edited message with the mention and triggers
6. Bridge auto-deletes the relay message after a short delay

No HTML parsing, no truncation, no content corruption.
Uses Telethon's formatting_entities for clean mention injection.

Requirements:
  pip install telethon

Usage:
  1. Copy .env.example to .env and fill in your values
  2. Run: python bridge.py
  3. First run will prompt for phone number authentication
  4. After auth, the session is saved and reused

For systemd service setup, see README.md
"""

import os
import json
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import InputMessageEntityMentionName
from telethon.errors import FloodWaitError

# ============================================================
# Configuration — loaded from environment variables
# ============================================================
def load_config():
    """Load configuration from environment or .env file."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key not in os.environ:
                    os.environ[key] = value

    config = {
        "api_id": int(os.environ.get("TG_API_ID", "0")),
        "api_hash": os.environ.get("TG_API_HASH", ""),
        "session_name": os.environ.get("TG_SESSION_NAME", "bridge_session"),
        "groups": [int(g.strip()) for g in os.environ.get("BRIDGE_GROUPS", "").split(",") if g.strip()],
        "bots": json.loads(os.environ.get("BRIDGE_BOTS", "[]")),
        "mention_silence": float(os.environ.get("MENTION_SILENCE", "5")),
        "delete_delay": float(os.environ.get("DELETE_DELAY", "2")),
        "edit_throttle": float(os.environ.get("EDIT_THROTTLE", "1.5")),
    }

    if not config["api_id"] or not config["api_hash"]:
        raise ValueError("TG_API_ID and TG_API_HASH are required. Get them from https://my.telegram.org/apps")
    if not config["groups"]:
        raise ValueError("BRIDGE_GROUPS is required. Set comma-separated MTProto channel IDs.")
    if not config["bots"]:
        raise ValueError("BRIDGE_BOTS is required. Set JSON array of bot configurations.")

    return config


# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRIDGE] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("bridge")


# ============================================================
# Relay file storage (optional — saves all relayed messages)
# ============================================================
RELAY_DIR = Path(__file__).parent / "relay"


def save_to_relay(sender_name, text, msg_id):
    """Save relayed message to file for record keeping."""
    RELAY_DIR.mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender": sender_name,
        "message_id": msg_id,
        "text": text,
    }
    with open(RELAY_DIR / "latest.json", "w") as f:
        json.dump(record, f, indent=2)
    with open(RELAY_DIR / "history.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


# ============================================================
# Bridge
# ============================================================
class TelegramBotBridge:
    """
    Multi-group bot-to-bot bridge using MTProto user account.

    Watches configured groups for bot messages. When a bot mentions
    another bot, relays the message with a proper mention entity
    so the target bot's Bot API can see it.
    """

    def __init__(self, config):
        self.config = config
        session_path = str(Path(__file__).parent / config["session_name"])
        self.client = TelegramClient(session_path, config["api_id"], config["api_hash"])
        self.group_entities = {}
        self.bot_input_entities = {}
        self.bot_config = {bot["username"]: bot for bot in config["bots"]}
        self.active = {}

    def _get_bot_username(self, sender):
        return (getattr(sender, "username", "") or "").lower()

    def _is_known_bot(self, username):
        """Check if a username matches any configured bot."""
        for bot_name, bot_info in self.bot_config.items():
            alts = bot_info.get("alts", [bot_name])
            if any(alt in username for alt in alts):
                return bot_name
        return None

    def _find_target_bots(self, text, sender_bot):
        """Find which configured bots are mentioned in the text."""
        targets = []
        lower = text.lower()
        for bot_name, bot_info in self.bot_config.items():
            if bot_name == sender_bot:
                continue
            alts = bot_info.get("alts", [bot_name])
            if any(alt in lower for alt in alts):
                targets.append(bot_name)
        return targets

    async def _safe_send(self, entity, text, reply_to=None):
        """Send message with FloodWait retry."""
        try:
            return await self.client.send_message(entity, text, reply_to=reply_to)
        except FloodWaitError as e:
            log.warning(f"FloodWait send: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            try:
                return await self.client.send_message(entity, text, reply_to=reply_to)
            except Exception:
                return None
        except Exception as e:
            log.error(f"Send failed: {e}")
            return None

    async def _safe_edit(self, entity, msg_id, text, formatting_entities=None):
        """Edit message with optional entities and FloodWait retry."""
        try:
            await self.client.edit_message(
                entity, msg_id, text,
                formatting_entities=formatting_entities
            )
            return True
        except FloodWaitError as e:
            log.warning(f"FloodWait edit: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            try:
                await self.client.edit_message(
                    entity, msg_id, text,
                    formatting_entities=formatting_entities
                )
                return True
            except Exception:
                return False
        except Exception as e:
            log.error(f"Edit failed: {e}")
            return False

    async def _safe_delete(self, entity, msg_id):
        """Delete message with FloodWait retry."""
        try:
            await self.client.delete_messages(entity, msg_id)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                await self.client.delete_messages(entity, msg_id)
            except Exception:
                log.error(f"Delete retry failed: {msg_id}")
        except Exception as e:
            log.error(f"Delete failed: {msg_id}: {e}")

    async def _add_mentions_and_delete(self, orig_msg_id, channel_id):
        """Wait for edit silence, add mention entities, then auto-delete."""
        await asyncio.sleep(self.config["mention_silence"])

        state = self.active.get(orig_msg_id)
        if not state or state.get("mentioned"):
            return

        entity = self.group_entities.get(channel_id)
        if not entity:
            return

        # Fetch final version of the message
        try:
            full_msg = await self.client.get_messages(entity, ids=orig_msg_id)
            if not full_msg or not full_msg.text:
                return
            final_text = full_msg.text
        except Exception as e:
            log.error(f"Fetch msg {orig_msg_id} failed: {e}")
            return

        sender_bot = state["sender_bot"]
        sender_name = state["sender_name"]
        targets = self._find_target_bots(final_text, sender_bot)

        if not targets:
            save_to_relay(f"@{sender_bot}", final_text, orig_msg_id)
            log.info(f"Filed msg {orig_msg_id} (no target bots mentioned)")
            self.active.pop(orig_msg_id, None)
            return

        save_to_relay(f"@{sender_bot}", final_text, orig_msg_id)

        # Relay to each target bot with proper mention entity
        for target_bot in targets:
            bot_info = self.bot_config.get(target_bot, {})
            mention_text = bot_info.get("mention")
            if not mention_text:
                continue

            input_entity = self.bot_input_entities.get(target_bot)
            if not input_entity:
                log.warning(f"No input entity for {target_bot}, skipping")
                continue

            prefix = f"{mention_text} [{sender_name}]: "
            relay_text = prefix + final_text
            if len(relay_text) > 4096:
                relay_text = relay_text[:4096]

            mention_entity = InputMessageEntityMentionName(
                offset=0,
                length=len(mention_text),
                user_id=input_entity
            )

            relay_id = state.get(f"relay_id_{target_bot}")
            if relay_id:
                ok = await self._safe_edit(
                    entity, relay_id, relay_text,
                    formatting_entities=[mention_entity]
                )
                if ok:
                    log.info(f"MENTION added for {target_bot} on msg {orig_msg_id} ({len(final_text)} chars)")
                    asyncio.create_task(
                        self._delete_relay(entity, relay_id, orig_msg_id, target_bot)
                    )

        state["mentioned"] = True

    async def _delete_relay(self, entity, relay_id, orig_msg_id, target_bot):
        """Delete relay message after configured delay."""
        await asyncio.sleep(self.config["delete_delay"])
        await self._safe_delete(entity, relay_id)
        log.info(f"DELETED relay for {target_bot} on msg {orig_msg_id}")
        state = self.active.get(orig_msg_id)
        if state:
            state.pop(f"relay_id_{target_bot}", None)
            if not any(k.startswith("relay_id_") for k in state):
                self.active.pop(orig_msg_id, None)

    async def handle_new(self, event, channel_id):
        """Handle new bot message — create relay without mention."""
        sender = await event.get_sender()
        if not sender or not getattr(sender, "bot", False):
            return

        username = self._get_bot_username(sender)
        sender_bot = self._is_known_bot(username)
        if not sender_bot:
            return

        text = event.message.text or ""
        if not text:
            return

        targets = self._find_target_bots(text, sender_bot)
        if not targets:
            return

        msg_id = event.message.id
        sender_name = getattr(sender, "first_name", "") or "Bot"
        entity = self.group_entities.get(channel_id)
        if not entity:
            return

        log.info(f"[NEW] msg {msg_id} from {sender_bot} -> {targets} ({len(text)} chars)")

        state = {
            "sender_bot": sender_bot,
            "sender_name": sender_name,
            "silence_task": None,
            "last_edit_mirror": time.time(),
            "mentioned": False,
            "channel_id": channel_id,
        }

        # Send relay WITHOUT mention for each target
        for target_bot in targets:
            relay_text = f"[{sender_name}]: {text}"
            sent = await self._safe_send(entity, relay_text, reply_to=msg_id)
            if sent:
                state[f"relay_id_{target_bot}"] = sent.id
                log.info(f"Relay sent for {target_bot} (msg_id={sent.id})")

        self.active[msg_id] = state
        state["silence_task"] = asyncio.create_task(
            self._add_mentions_and_delete(msg_id, channel_id)
        )

    async def handle_edit(self, event, channel_id):
        """Handle bot message edit — mirror to relay, reset silence timer."""
        msg_id = event.message.id
        state = self.active.get(msg_id)
        if not state or state.get("mentioned"):
            return

        text = event.message.text or ""
        if not text:
            return

        log.info(f"[EDIT] msg {msg_id} ({len(text)} chars)")

        entity = self.group_entities.get(channel_id)
        if not entity:
            return

        # Throttle mirror edits
        now = time.time()
        if now - state["last_edit_mirror"] >= self.config["edit_throttle"]:
            sender_name = state.get("sender_name", "Bot")
            relay_text = f"[{sender_name}]: {text}"
            for key, relay_id in list(state.items()):
                if key.startswith("relay_id_"):
                    await self._safe_edit(entity, relay_id, relay_text)
            state["last_edit_mirror"] = now

        # Reset silence timer
        if state["silence_task"]:
            state["silence_task"].cancel()
        state["silence_task"] = asyncio.create_task(
            self._add_mentions_and_delete(msg_id, channel_id)
        )

    async def start(self):
        """Start the bridge."""
        config = self.config

        await self.client.connect()

        if not await self.client.is_user_authorized():
            log.info("First run — authenticating...")
            phone = input("Enter your phone number: ")
            await self.client.send_code_request(phone)
            code = input("Enter the code you received: ")
            try:
                await self.client.sign_in(phone, code)
            except Exception:
                password = input("Enter 2FA password: ")
                await self.client.sign_in(password=password)

        me = await self.client.get_me()
        log.info(f"Logged in as: {me.first_name} (ID: {me.id})")

        # Resolve group entities
        for ch_id in config["groups"]:
            try:
                entity = await self.client.get_entity(ch_id)
                self.group_entities[ch_id] = entity
                title = getattr(entity, "title", str(ch_id))
                log.info(f"Group: {title} ({ch_id})")
            except Exception as e:
                log.error(f"Failed to resolve group {ch_id}: {e}")

        # Resolve bot entities for mentions
        for bot_name, bot_info in self.bot_config.items():
            if bot_info.get("mention"):
                try:
                    input_entity = await self.client.get_input_entity(bot_name)
                    self.bot_input_entities[bot_name] = input_entity
                    log.info(f"Bot entity resolved: {bot_name}")
                except Exception as e:
                    log.warning(f"Could not resolve {bot_name}: {e}")

        log.info(f"Watching {len(self.group_entities)} groups, {len(self.bot_input_entities)} bot entities")
        log.info(f"Silence: {config['mention_silence']}s, delete: {config['delete_delay']}s, throttle: {config['edit_throttle']}s")

        all_channels = list(self.group_entities.keys())

        @self.client.on(events.NewMessage(chats=all_channels))
        async def on_new(event):
            chat = await event.get_chat()
            channel_id = getattr(chat, "id", None)
            await self.handle_new(event, channel_id)

        @self.client.on(events.MessageEdited(chats=all_channels))
        async def on_edit(event):
            chat = await event.get_chat()
            channel_id = getattr(chat, "id", None)
            await self.handle_edit(event, channel_id)

        log.info("Bridge is LIVE")
        await self.client.run_until_disconnected()


async def main():
    config = load_config()
    bridge = TelegramBotBridge(config)
    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())
