"""Tools the model may call. Server tools run here; client tools run in the desktop app.

A client tool call is sent to the app as a `client_action` event and the loop waits for
POST /v1/tasks/{id}/tool-results. The app tells the server which client tools it can run.
"""

from __future__ import annotations

from app.llm.tools import TOOLS as SERVER_TOOLS


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


_CLIENT: dict[str, dict] = {}


def register_client_tool(schema: dict) -> None:
    """Other modules (PC actions, skills) add their client tools here."""
    _CLIENT[schema["function"]["name"]] = schema


for _schema in (
    _fn(
        "browser_open",
        "Открыть адрес во вкладке браузера на компьютере пользователя. Там уже есть его входы в сервисы. "
        "Возвращает адрес, заголовок и текст страницы с пронумерованными элементами.",
        {"url": {"type": "string", "description": "Адрес, http или https"}},
        ["url"],
    ),
    _fn(
        "browser_read",
        "Прочитать текущую страницу браузера: адрес, заголовок, текст и пронумерованные кнопки, ссылки и поля.",
        {},
    ),
    _fn(
        "browser_click",
        "Нажать на элемент страницы. Укажи номер элемента из browser_read (ref) или видимый текст кнопки или ссылки.",
        {
            "ref": {"type": "integer", "description": "Номер элемента из последнего чтения страницы"},
            "text": {"type": "string", "description": "Видимый текст элемента, если номера нет"},
        },
    ),
    _fn(
        "browser_type",
        "Ввести текст в поле. Укажи номер поля (ref) из browser_read. submit=true нажмёт Enter после ввода.",
        {
            "ref": {"type": "integer", "description": "Номер поля из последнего чтения страницы"},
            "text": {"type": "string", "description": "Что ввести"},
            "submit": {"type": "boolean", "description": "Нажать Enter после ввода"},
        },
        ["text"],
    ),
    _fn(
        "browser_press",
        "Нажать клавишу в браузере: Enter, Escape, Tab, PageDown, PageUp, ArrowDown, ArrowUp.",
        {"key": {"type": "string"}},
        ["key"],
    ),
    _fn(
        "browser_screenshot",
        "Сделать снимок вкладки, чтобы пользователь видел, что происходит. Модели возвращается только подтверждение.",
        {},
    ),
    _fn(
        "ask_confirmation",
        "Спросить пользователя перед действием, которое трудно отменить: отправка письма или сообщения, оплата, "
        "удаление, изменение данных клиента, публикация. Не выполняй такое действие без ответа approved.",
        {
            "action": {"type": "string", "description": "Коротко, что сделать"},
            "details": {"type": "string", "description": "Кому, что именно, сколько"},
        },
        ["action"],
    ),
):
    register_client_tool(_schema)

try:  # PC actions live in another module; the loop works without it.
    from app.computer import COMPUTER_TOOL  # type: ignore[import-not-found]

    register_client_tool(COMPUTER_TOOL)
except Exception:  # noqa: BLE001
    pass

SERVER_TOOL_NAMES = {tool["function"]["name"] for tool in SERVER_TOOLS}


def client_tool_names() -> set[str]:
    return set(_CLIENT)


def tools_for(client_tools: list[str] | set[str] | None) -> list[dict]:
    """Server tools plus the client tools this app said it can run."""
    wanted = set(client_tools or ())
    return list(SERVER_TOOLS) + [schema for name, schema in _CLIENT.items() if name in wanted]
