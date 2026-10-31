"""Outbound notifications. Telegram is filled in when a bot token is configured."""

import logging
import os

log = logging.getLogger(__name__)


def notify(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if not token or not chat:
        log.info("notification skipped")
        return False
    return _deliver(token, chat, text)


def _deliver(token: str, chat: str, text: str) -> bool:
    log.info("notification pending")
    return False
