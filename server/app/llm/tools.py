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
                "Поиск в интернете. Возвращает заголовки, ссылки и короткие фрагменты. "
                "Используй для свежих фактов, ссылок на сервисы, цен, новостей."
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
                "Открыть страницу по адресу и прочитать её текст и ссылки. "
                "Используй, чтобы проверить ссылку или прочитать сайт, найденный поиском или известный тебе."
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


def parse_args(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def call_tool(name: str, args: dict) -> tuple[str, str | None]:
    """Run one tool. Returns (text for the model, error code or None)."""
    try:
        if name == "web_search":
            hits = web_search(str(args.get("query", "")))
            if not hits:
                return "Ничего не найдено.", None
            lines = [f"{i}. {hit.title}\n{hit.url}\n{hit.snippet}" for i, hit in enumerate(hits, 1)]
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
