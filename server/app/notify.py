"""Telegram notifications. Without a bot token the call is skipped."""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def notify(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if not token or not chat:
        log.info("notification skipped")
        return False
    try:
        _deliver(token, chat, text)
    except Exception:
        log.info("notification failed")
        return False
    log.info("notification sent")
    return True


def _deliver(token: str, chat: str, text: str) -> None:
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text},
        timeout=10,
    )
    response.raise_for_status()
