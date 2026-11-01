"""Навыки агента для 1С, Битрикс24 и amoCRM.

Цикл агента зовёт три функции:
- guide_for(request_text, url) — блок подсказок для системного промпта;
- login_steps(session, user, app_id, url) — шаги входа с плейсхолдером пароля;
- resolve_secret_args(session, user, args, current_url) — подстановка пароля перед
  отправкой действия в браузер. Модель и траектория видят только плейсхолдер.
"""

from __future__ import annotations

import re

from app.skills.hosts import host_matches, host_of
from app.skills.login import (
    SecretRefused,
    credential_for,
    login_steps,
    placeholder,
    resolve_secret_args,
)
from app.skills.playbooks import GENERAL_RULES, PLAYBOOKS, Playbook, playbook

__all__ = [
    "PLAYBOOKS",
    "Playbook",
    "SecretRefused",
    "credential_for",
    "detect",
    "guide_for",
    "login_steps",
    "placeholder",
    "playbook",
    "resolve_secret_args",
]

_PATTERNS = {
    book.id: re.compile("|".join(f"(?<![\\w])(?:{kw})" for kw in book.keywords), re.IGNORECASE)
    for book in PLAYBOOKS
}


def detect(request_text: str = "", url: str | None = None) -> list[Playbook]:
    """Системы, к которым относится шаг: сначала по адресу, потом по тексту."""
    found: list[Playbook] = []
    host = host_of(url)
    lowered = (url or "").lower()
    for book in PLAYBOOKS:
        by_host = any(host_matches(host, suffix) for suffix in book.host_suffixes)
        by_marker = any(marker in lowered for marker in book.url_markers)
        if by_host or by_marker:
            found.append(book)
    for book in PLAYBOOKS:
        if book not in found and _PATTERNS[book.id].search(request_text or ""):
            found.append(book)
    return found


def guide_for(request_text: str = "", url: str | None = None) -> str | None:
    books = detect(request_text, url)
    if not books:
        return None
    parts = [f"## {book.name}\n{book.guide}" for book in books]
    return "\n\n".join([*parts, GENERAL_RULES])
