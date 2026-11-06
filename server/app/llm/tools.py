"""Tools the agent may call during a chat turn. Each returns plain text for the model."""

from __future__ import annotations

import json

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
            hits = web_search(str(args.get("query", "")))
            if not hits:
                return "Ничего не найдено.", None
            hits = sorted(hits, key=lambda hit: official_rank(hit.url))  # stable: search order within a group
            lines = [
                f"{i}. {hit.title}{' (официальный источник)' if official_rank(hit.url) == 0 else ''}\n{hit.url}\n{hit.snippet}"
                for i, hit in enumerate(hits, 1)
            ]
            return "\n\n".join(lines), None
        if name == "open_url":
            page = fetch_page(str(args.get("url", "")))
            parts = [f"Адрес: {page.url}"]
            if page.title:
                parts.append(f"Заголовок: {page.title}")
            parts.append(page.text or "(текста на странице нет)")
            if page.links:
                parts.append("Ссылки:\n" + "\n".join(f"- {label}: {href}" for label, href in page.links))
            return "\n\n".join(parts), None
    except WebError as exc:
        return f"Ошибка: {exc.code}", exc.code
    return f"Ошибка: неизвестный инструмент {name}", "unknown_tool"
