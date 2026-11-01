"""Вход в рабочие системы через vault.

Модель получает шаги, где пароль — плейсхолдер <CREDENTIAL:uuid>. Настоящий
секрет подставляет resolve_secret_args прямо перед отправкой действия в браузер,
и только если браузер сейчас на сайте этого доступа. Так подсказка на чужой
странице не уведёт пароль: на другом домене подстановка отказывает.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Credential, User
from app.skills.hosts import host_matches, host_of
from app.skills.playbooks import PLAYBOOKS, Playbook, playbook
from app.vault import secret_for_browser

_PLACEHOLDER = re.compile(r"<CREDENTIAL:([0-9a-fA-F-]{36})>")
# Пароль можно вводить только в поле, не в адрес и не в клик.
_SECRET_TOOLS = {"browser_type"}
_SECRET_KEYS = {"text"}


class SecretRefused(Exception):
    """Подстановка запрещена. code уходит в результат шага, секрет — нет."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def placeholder(credential_id: uuid.UUID | str) -> str:
    return f"<CREDENTIAL:{uuid.UUID(str(credential_id))}>"


def _site_url(site: str) -> str:
    site = site.strip()
    return site if "://" in site else f"https://{site}"


def _book_for_site(site: str) -> Playbook | None:
    host = host_of(site)
    for book in PLAYBOOKS:
        if any(host_matches(host, suffix) for suffix in book.host_suffixes):
            return book
    return None


def credential_for(
    session: Session,
    user: User,
    app_id: str | None = None,
    url: str | None = None,
) -> dict | None:
    """Доступ пользователя для системы или адреса. Без секрета: id, site, username."""
    rows = list(
        session.scalars(
            select(Credential).where(Credential.user_id == user.id).order_by(Credential.created_at)
        )
    )
    target = host_of(url)
    if target:
        for row in rows:
            site = host_of(row.site)
            if site and (host_matches(target, site) or host_matches(site, target)):
                return _public(row)
    book = playbook(app_id) if app_id else None
    if book is not None:
        for row in rows:
            if _book_for_site(row.site) is book:
                return _public(row)
    return None


def login_steps(
    session: Session,
    user: User,
    app_id: str,
    url: str | None = None,
) -> list[dict]:
    """Шаги входа для цикла агента. Пустой список — доступа нет, надо попросить пользователя."""
    book = playbook(app_id)
    if book is None:
        return []
    cred = credential_for(session, user, app_id, url)
    if cred is None:
        return []
    form = book.login
    start = url or _site_url(cred["site"])
    steps: list[dict] = [
        {"tool": "browser_open", "args": {"url": start}},
        {"tool": "browser_screenshot", "args": {}, "note": "Если уже вошли, дальше не нужно."},
        _type(form.username_selectors, cred["username"]),
    ]
    if form.two_step:
        steps.append(_click(form.next_selectors))
    steps += [
        _type(form.password_selectors, placeholder(cred["id"])),
        _click(form.submit_selectors),
        {
            "tool": "browser_screenshot",
            "args": {},
            "note": "Капча, код из SMS или второй фактор — остановись и попроси пользователя.",
        },
    ]
    return steps


def resolve_secret_args(
    session: Session,
    user: User,
    tool: str,
    args: dict[str, Any],
    current_url: str | None,
) -> dict[str, Any]:
    """Копия args с настоящим паролем. Результат не логировать и не сохранять."""
    hits = [(key, value) for key, value in args.items() if isinstance(value, str) and _PLACEHOLDER.search(value)]
    if not hits:
        return args
    if tool not in _SECRET_TOOLS:
        raise SecretRefused("secret_wrong_tool")
    resolved = dict(args)
    for key, value in hits:
        match = _PLACEHOLDER.fullmatch(value.strip())
        if key not in _SECRET_KEYS or match is None:
            raise SecretRefused("secret_wrong_field")
        cred_id = uuid.UUID(match.group(1))
        row = session.get(Credential, cred_id)
        if row is None or row.user_id != user.id:
            raise SecretRefused("secret_not_found")
        if not _host_allowed(row.site, current_url):
            raise SecretRefused("secret_wrong_site")
        resolved[key] = secret_for_browser(session, user, cred_id)
    return resolved


def _host_allowed(site: str, current_url: str | None) -> bool:
    current = host_of(current_url)
    site_host = host_of(site)
    if not current or not site_host:
        return False
    if host_matches(current, site_host):
        return True
    book = _book_for_site(site)
    return book is not None and any(host_matches(current, auth) for auth in book.auth_hosts)


def _type(selectors: tuple[str, ...], text: str) -> dict:
    return {
        "tool": "browser_type",
        "args": {"selector": selectors[0], "text": text},
        "alternatives": list(selectors[1:]),
    }


def _click(selectors: tuple[str, ...]) -> dict:
    return {
        "tool": "browser_click",
        "args": {"selector": selectors[0]},
        "alternatives": list(selectors[1:]),
    }


def _public(row: Credential) -> dict:
    return {"id": str(row.id), "site": row.site, "username": row.username}
