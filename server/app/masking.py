"""Mask secrets and personal data before anything is stored or exported.

The fine-tune copy must not contain passwords, payment data, or direct
identifiers. Upstream model names are stripped so they cannot leak into an
export or an API payload.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cvv",
    "cvc",
    "pin",
    "access_token",
    "refresh_token",
    "heli_api_key",
}

# Server-side denylist. These words are not shown in the client, docs, or public schema.
_UPSTREAM_WORDS = (
    "deepseek",
    "anthropic",
    "claude",
    "gemini",
    "openai",
    "gpt-4",
    "gpt-5",
    "qwen",
    "mistral",
)

# Unicode-aware: Cyrillic addresses (петя@почта.рф) and punycode zones (xn--p1ai) are masked too.
_EMAIL = re.compile(r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.(?:xn--[\w\-]+|[^\W\d_]{2,})\b", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
_INN = re.compile(r"(?i)(инн)\s*[:№#]?\s*(\d{10}|\d{12})")
_PASSPORT = re.compile(r"(?i)(паспорт)\s*[:№#]?\s*(\d{4})[\s\-]?(\d{6})")
_SNILS = re.compile(r"(?i)(снилс)\s*[:№#]?\s*\d{3}[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{2}")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+=*")
_TEXT_LIMIT = 200_000
# «Пароль: Abc123!», «**пароль** — `x9!k`», «password=...»: the value after the word goes.
_PASSWORD = re.compile(
    r"(?i)(?<![\w])(пароль|паролем|пароля|password|passwd|pwd)"
    r"((?:\*\*|__)?\s*(?:[:=]|—|–|-{1,2}|\s(?:это|is)\s)\s*(?:\*\*|__)?\s*)"
    r"([`'\"«]?)(?!<CREDENTIAL)(?!•)([^\s`'\"»<]{4,})([`'\"»]?)"
)
_PASSWORD_QUOTED = re.compile(
    r"(?i)(?<![\w])(пароль|паролем|пароля|password)(\s+)([`«\"'])(?!<CREDENTIAL)([^`»\"'\s]{4,})([`»\"'])"
)
PASSWORD_MASK = "••••••••"

_redact_filter_installed = False
_log_factory = logging.getLogRecordFactory()


def _redacting_factory(*args, **kwargs):
    record = _log_factory(*args, **kwargs)
    secret = os.environ.get("HELI_API_KEY") or ""
    if not secret:
        return record
    try:
        message = record.getMessage()
    except Exception:
        return record
    if secret in message:
        record.msg = message.replace(secret, "[redacted]")
        record.args = None
    return record


def _luhn(number: str) -> bool:
    digits = [int(ch) for ch in number if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    alt = False
    for digit in reversed(digits):
        if alt:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        alt = not alt
    return total % 10 == 0


def _mask_cards(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        if _luhn(raw):
            return "[card]"
        return raw

    return _CARD.sub(replace, text)


def _mask_upstream(text: str) -> str:
    masked = text
    for word in _UPSTREAM_WORDS:
        masked = re.sub(re.escape(word), "[model]", masked, flags=re.IGNORECASE)
    return masked


def mask_passwords(text: str) -> str:
    """Hide a password written out in text (an answer, a note, a typed value): keep the word, drop the value."""
    if not text or not isinstance(text, str):
        return text
    swap = lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{PASSWORD_MASK}{m.group(5)}"  # noqa: E731
    return _PASSWORD_QUOTED.sub(swap, _PASSWORD.sub(swap, text))


def redact_secrets_text(text: str) -> str:
    """Drop credentials only. Personal data stays until the training copy is built."""
    if not text:
        return text
    secret = os.environ.get("HELI_API_KEY") or ""
    masked = text
    if secret and secret in masked:
        masked = masked.replace(secret, "[redacted]")
    return mask_passwords(_BEARER.sub("Bearer [redacted]", masked))


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets_text(value)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                cleaned[key] = "<CREDENTIAL>"
            else:
                cleaned[key] = redact_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def mask_text(text: str) -> str:
    if not text:
        return text
    secret = os.environ.get("HELI_API_KEY") or ""
    masked = text
    if secret and secret in masked:
        masked = masked.replace(secret, "[redacted]")
    masked = mask_passwords(_BEARER.sub("Bearer [redacted]", masked))
    masked = _EMAIL.sub("[email]", masked)
    masked = _PHONE.sub("[phone]", masked)
    masked = _INN.sub(r"\1 [inn]", masked)
    masked = _PASSPORT.sub(r"\1 [passport]", masked)
    masked = _SNILS.sub(r"\1 [snils]", masked)
    masked = _mask_cards(masked)
    masked = _mask_upstream(masked)
    if len(masked) > _TEXT_LIMIT:
        masked = masked[:_TEXT_LIMIT]
    return masked


def mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                cleaned[key] = "<CREDENTIAL>"
            elif str(key).lower() in {"email", "phone", "user_id", "userid"}:
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = mask_value(item)
        return cleaned
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    return value


class RedactLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secret = os.environ.get("HELI_API_KEY") or ""
        if not secret:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if secret in message:
            record.msg = message.replace(secret, "[redacted]")
            record.args = ()
        return True


def install_redact_filter() -> None:
    global _redact_filter_installed
    if _redact_filter_installed:
        return
    logging.setLogRecordFactory(_redacting_factory)
    logging.getLogger().addFilter(RedactLogFilter())
    _redact_filter_installed = True
