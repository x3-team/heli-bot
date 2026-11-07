"""Tools the agent may call during a chat turn. Each returns plain text for the model."""

from __future__ import annotations

import json
import re

from app.web import WebError, fetch_page, web_search

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Поиск в интернете. Возвращает заголовки, ссылки и короткие фрагменты; официальная справка и "
                "документация API сервисов идут первыми (помечены «официальный источник»). "
                "Используй для свежих фактов, правил и ограничений сервисов, ссылок, цен, новостей. "
                "Для правил сервиса добавляй в запрос «справка» или «документация API»."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Открыть страницу по адресу и прочитать её текст и ссылки (fetch_page). "
                "Используй, чтобы прочитать справку или документацию, проверить ссылку или сайт, найденный "
                "поиском или известный тебе."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Адрес страницы, http или https"},
                    "fresh": {
                        "type": "boolean",
                        "description": "true — не брать копию из кеша, а прочитать страницу заново (свежие цены, новости)",
                    },
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}
# Other names the model may use for the same tools.
ALIASES = {"fetch_page": "open_url", "read_page": "open_url"}

# Official help and API docs of services Heli works with, and of the state. Ranked first in search.
OFFICIAL_SOURCES = (
    "yandex.ru/support", "yandex.ru/dev", "yandex.com/dev", "yandex.cloud/ru/docs", "cloud.yandex.ru/docs",
    "docs.ozon.ru", "seller-edu.ozon.ru", "dev.wildberries.ru", "openapi.wildberries.ru", "seller.wildberries.ru/instructions",
    "dev.moysklad.ru", "support.moysklad.ru", "apidocs.bitrix24.ru", "dev.1c-bitrix.ru", "helpdesk.bitrix24.ru",
    "its.1c.ru", "v8.1c.ru", "amocrm.ru/developers", "www.amocrm.ru/support", "vk.com/dev", "dev.vk.com", "ads.vk.com/help",
    "core.telegram.org", "developers.google.com", "support.google.com", "nalog.gov.ru", "cbr.ru", "consultant.ru",
    "garant.ru", "gosuslugi.ru", "pravo.gov.ru",
)
_DOCS_HOST = ("docs.", "developer.", "developers.", "dev.", "help.", "support.", "api.")


def official_rank(url: str) -> int:
    """0 — official help or API docs, 1 — anything else."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return 1
    host = (parts.hostname or "").lower().removeprefix("www.")
    where = host + (parts.path or "")
    if any(where.startswith(src.removeprefix("www.")) for src in OFFICIAL_SOURCES):
        return 0
    return 0 if host.startswith(_DOCS_HOST) else 1


def parse_args(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def call_tool(name: str, args: dict) -> tuple[str, str | None]:
    """Run one tool. Returns (text for the model, error code or None)."""
    name = ALIASES.get(name, name)
    try:
        if name == "web_search":
            query = str(args.get("query", ""))
            hits = web_search(query)
            hint = _weak_hint(query, hits)
            if not hits:
                return "Ничего не найдено." + hint, None
            hits = sorted(hits, key=lambda hit: official_rank(hit.url))  # stable: search order within a group
            lines = [
                f"{i}. {hit.title}{' (официальный источник)' if official_rank(hit.url) == 0 else ''}\n{hit.url}\n{hit.snippet}"
                for i, hit in enumerate(hits, 1)
            ]
            return "\n\n".join(lines) + hint, None
        if name == "open_url":
            return _open_url(str(args.get("url", "")), bool(args.get("fresh"))), None
    except WebError as exc:
        return f"Ошибка: {exc.code}", exc.code
    return f"Ошибка: неизвестный инструмент {name}", "unknown_tool"


WEAK_HINT = (
    "\n\nВыдача слабая: общего веб-поиска без ключа нет, ищутся справки сервисов, прочитанные страницы, "
    "Викиданные и Википедия. Открой официальный сайт сам (open_url или browser_open; адрес — из результатов "
    "выше, из Викиданных или известный тебе) и ищи на нём: разделы «Цены», «Тарифы», «Помощь», поиск по сайту."
)
# Query parameters that may carry a secret: such pages are never cached.
_SECRET_PARAM = re.compile(r"(^|&)(token|key|apikey|api_key|sig|signature|auth|session|sid|code|password)=", re.I)


def _weak_hint(query: str, hits) -> str:
    from app.search import is_weak

    return WEAK_HINT if is_weak(hits) else ""


def _cacheable(url: str) -> bool:
    from urllib.parse import urlsplit

    return not _SECRET_PARAM.search(urlsplit(url).query or "")


def _render(url: str, title: str, body: str, note: str = "") -> str:
    parts = [f"Адрес: {url}" + note]
    if title:
        parts.append(f"Заголовок: {title}")
    parts.append(body or "(текста на странице нет)")
    return "\n\n".join(parts)


def _open_url(url: str, fresh: bool) -> str:
    """Read a page, through the cache of public pages. Raises WebError."""
    from app.search import cache

    normal = url.strip()
    if normal and "://" not in normal:
        normal = "https://" + normal
    if not fresh and _cacheable(normal):
        with cache.cache_session() as session:
            page = cache.get(session, normal) if session is not None else None
            if page is not None and cache.is_fresh(page):
                cache.touch(session, page)
                note = f" (копия из кеша от {page.fetched_at:%d.%m.%Y}; fresh=true — прочитать заново)"
                return _render(page.url, page.title, page.text, note)
    page = fetch_page(normal)
    body = page.text
    if page.links:
        body += "\n\nСсылки:\n" + "\n".join(f"- {label}: {href}" for label, href in page.links)
    if _cacheable(page.url):
        with cache.cache_session() as session:
            if session is not None:
                cache.store(session, page.url, page.title, body, source="agent")
    return _render(page.url, page.title, body)
