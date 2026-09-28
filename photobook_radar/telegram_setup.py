"""One-time interactive setup; never records the token in the repository."""
from __future__ import annotations

import getpass
import json
import os
import re
import tomllib
from pathlib import Path

import httpx

from .config import load_config
from .notifications import private_secrets


def setup() -> None:
    config = load_config()
    path = config.data_dir / "secrets.toml"
    existing = {}
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise PermissionError("Existing secrets file must be owner-only (chmod 600)")
        existing = tomllib.loads(path.read_text()).get("telegram", {})
        if existing.get("chat_id"):
            raise RuntimeError("Telegram chat is already configured; edit the private secrets file if rotating it")
    token = str(existing.get("bot_token") or getpass.getpass("Telegram bot token from BotFather (hidden): ")).strip()
    if ":" not in token or any(ch.isspace() for ch in token):
        raise ValueError("That does not look like a Telegram bot token")
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            me = client.get(f"https://api.telegram.org/bot{token}/getMe")
            me.raise_for_status()
            if me.json().get("ok") is not True:
                raise ValueError("Telegram did not accept the bot token")
            updates = client.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"limit": 100, "timeout": 0})
            updates.raise_for_status()
            if updates.json().get("ok") is not True:
                raise ValueError("Telegram could not read the bot's recent messages")
            chats = {}
            for entry in updates.json().get("result", []):
                message = entry.get("message") or entry.get("edited_message") or {}
                chat = message.get("chat") or {}
                if chat.get("type") == "private" and chat.get("id") is not None:
                    chats[str(chat["id"])] = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip() or str(chat.get("username") or "private chat")
    except httpx.HTTPError:
        raise RuntimeError("Telegram API request failed. Check the bot token, network and bot settings; no token was saved") from None
    if not chats:
        raise RuntimeError("No private chat found. Open your new bot in Telegram, send /start, then run setup again")
    print("Private chats that have messaged this bot:")
    for index, (chat_id, label) in enumerate(chats.items(), start=1):
        print(f"  {index}. {label} ({chat_id})")
    selection = "1" if len(chats) == 1 else input("Number for your own phone chat: ").strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(chats):
        raise ValueError("No chat selected; nothing saved")
    chat_id = list(chats)[int(selection) - 1]
    if existing.get("bot_token"):
        current = path.read_text()
        match = re.search(r"(?m)^\[telegram\]\s*$", current)
        if not match:
            raise RuntimeError("Private secrets file has no Telegram section")
        next_section = re.search(r"(?m)^\[[^]]+\]", current[match.end():])
        insert_at = match.end() + next_section.start() if next_section else len(current)
        payload = current[:insert_at].rstrip() + "\nchat_id = " + json.dumps(chat_id) + "\n\n" + current[insert_at:].lstrip("\n")
    else:
        payload = (path.read_text() if path.exists() else "") + "\n[telegram]\nbot_token = " + json.dumps(token) + "\nchat_id = " + json.dumps(chat_id) + "\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.new")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    print("Telegram credentials saved locally. No alerts will send until production cutover and a phone test.")


def send_test() -> dict:
    """Send one labelled configuration test, independent of production mode."""
    config = load_config()
    secret = private_secrets(config).get("telegram", {})
    token, chat_id = secret.get("bot_token"), secret.get("chat_id")
    if not token or not chat_id:
        raise RuntimeError("Telegram bot and private chat must be configured first")
    payload = {"chat_id": str(chat_id), "text": "Photobook Radar setup test. This is a test message, not a book recommendation. Live scanning and automatic alerts are still off.", "disable_web_page_preview": True}
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError:
        raise RuntimeError("Telegram did not accept the test message; token details were not logged") from None
    if result.get("ok") is not True:
        raise RuntimeError("Telegram rejected the test message")
    return {"provider_accepted": True, "message_id": result.get("result", {}).get("message_id"), "phone_receipt": "awaiting owner confirmation"}
