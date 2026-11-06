"""Битрикс24 REST through the user's incoming webhook: deals, leads, contacts, tasks, users.

bitrix24_read reads on any portal: exact data for answers and a check of a record after «Сохранить» on the
screen. Without a login on the screen (the sign-in page asks a captcha from the server) read-only tasks can
be done through this tool alone.

bitrix24_write creates and changes records (deals, leads, contacts, companies, tasks, comments, stages),
but only on a training portal: SANDBOX_PORTALS plus HELI_BITRIX_SANDBOX_HOSTS. A live portal stays read-only
through REST, the same rule as the Direct API and http_request: changes there go through the screen, where
the user sees them and confirms the dangerous ones. Nothing deletes through REST, sandbox or not.

Access: «Доступы» entry with site = the portal (company.bitrix24.ru or a box on its own domain), login
«webhook», password = the incoming webhook URL (https://company.bitrix24.ru/rest/1/abc123/) or just «1/abc123».
The call goes only to that portal's host, never to a host taken from the secret.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid

import httpx

from app.agent.control import ClientResult

log = logging.getLogger(__name__)

TIMEOUT_S = 20
RESULT_CHARS = 8000
MAX_ROWS = 50
WEBHOOK_LOGINS = {"webhook", "вебхук", "rest"}

# Only methods that read. Anything else is refused before a request is made.
METHODS = {
    "crm.deal.list", "crm.deal.get", "crm.deal.fields",
    "crm.lead.list", "crm.lead.get", "crm.lead.fields",
    "crm.contact.list", "crm.contact.get",
    "crm.company.list", "crm.company.get",
    "crm.activity.list", "crm.activity.get",
    "crm.timeline.comment.list",
    "crm.status.list", "crm.category.list", "crm.dealcategory.list", "crm.dealcategory.stage.list",
    "crm.item.list", "crm.item.get",
    "crm.product.list", "crm.product.get",
    "crm.invoice.list",
    "tasks.task.list", "tasks.task.get", "task.commentitem.getlist", "task.elapseditem.getlist",
    "user.get", "user.current", "department.get",
    "calendar.event.get",
    "im.recent.list",
    "user.search",
}
# Methods that create or change. Only on a training portal (is_sandbox); deletes are not here at all.
WRITE_METHODS = {
    "crm.deal.add", "crm.deal.update",
    "crm.lead.add", "crm.lead.update",
    "crm.contact.add", "crm.contact.update",
    "crm.company.add", "crm.company.update",
    "crm.timeline.comment.add", "crm.activity.add", "crm.activity.todo.add",
    "tasks.task.add", "tasks.task.update", "tasks.task.complete", "tasks.task.start", "tasks.task.renew",
    "task.commentitem.add",
}
# Training portals (demo, trial). Everything else is a live portal of a client: read only.
SANDBOX_PORTALS = ("b24-lvsc4f.bitrix24.ru",)
_SECRET = re.compile(r"(?:/rest/)?(\d+)/([A-Za-z0-9_]+)/?$")


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_PARAMS = {
    "type": "object",
    "description": "Параметры метода: filter, select, order, id, taskId, start, fields. "
    "Пример: {\"filter\": {\"STAGE_ID\": \"NEW\"}, \"select\": [\"ID\", \"TITLE\", \"OPPORTUNITY\"]}",
}
_PORTAL = {"type": "string", "description": "Адрес портала, если у пользователя их несколько"}

TOOL = _fn(
    "bitrix24_read",
    "Прочитать данные Битрикс24 через REST (входящий вебхук пользователя в «Доступах»): сделки, лиды, "
    "контакты, компании, задачи, пользователи. Если вебхук есть — это первый путь для любой задачи Битрикс24, "
    "вход на экране не нужен. Проверяй им запись после создания или изменения. "
    "Методы: " + ", ".join(sorted(METHODS)) + ".",
    {
        "method": {"type": "string", "description": "Метод REST, например crm.deal.list или tasks.task.get"},
        "params": _PARAMS,
        "portal": _PORTAL,
    },
    ["method"],
)
WRITE_TOOL = _fn(
    "bitrix24_write",
    "Создать или изменить запись Битрикс24 через REST (вебхук в «Доступах»), только на учебном портале: "
    "сделка, лид, контакт, компания, задача, комментарий, смена стадии (crm.deal.update с fields.STAGE_ID). "
    "На боевом портале клиента REST только читает: там меняй на экране. Удаления нет. "
    "После записи проверь её через bitrix24_read (crm.deal.get, tasks.task.get). "
    "Пример: method tasks.task.add, params {\"fields\": {\"TITLE\": \"…\", \"RESPONSIBLE_ID\": 1, "
    "\"DEADLINE\": \"2026-11-04T18:00:00+03:00\"}}. Методы: " + ", ".join(sorted(WRITE_METHODS)) + ".",
    {
        "method": {"type": "string", "description": "Метод REST, например crm.deal.add или tasks.task.update"},
        "params": _PARAMS,
        "portal": _PORTAL,
    },
    ["method"],
)
TOOL_NAMES = ("bitrix24_read", "bitrix24_write")


class BxError(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.text = text


def is_webhook_login(username: str | None) -> bool:
    return str(username or "").strip().lower() in WEBHOOK_LOGINS


def webhook_base(site: str, secret: str) -> str:
    """https://<portal>/rest/<user>/<code>/ from the credential's site and its secret."""
    from app.skills.hosts import host_of

    host = host_of(site)
    if not host:
        raise BxError("bad_credential", "Ошибка: в «Доступах» у вебхука Битрикс24 не указан адрес портала.")
    match = _SECRET.search(secret.strip())
    if not match:
        raise BxError(
            "bad_credential",
            "Ошибка: пароль вебхука Битрикс24 в «Доступах» должен быть адресом вида https://портал/rest/1/код/.",
        )
    return f"https://{host}/rest/{match.group(1)}/{match.group(2)}/"


def sandbox_portals() -> set[str]:
    extra = os.environ.get("HELI_BITRIX_SANDBOX_HOSTS", "")
    return {h.strip().lower() for h in (*SANDBOX_PORTALS, *extra.split(",")) if h.strip()}


def is_sandbox(site: str) -> bool:
    from app.skills.hosts import host_of

    host = host_of(site)
    return bool(host) and host in sandbox_portals()


def build_call(args: dict, write: bool = False) -> tuple[str, dict]:
    method = str(args.get("method") or "").strip().lower()
    allowed = WRITE_METHODS if write else METHODS
    if method not in allowed:
        if not write and method in WRITE_METHODS:
            raise BxError("bad_method", "Ошибка: этот метод меняет данные — вызывай его через bitrix24_write.")
        if write and method in METHODS:
            raise BxError("bad_method", "Ошибка: этот метод читает — вызывай его через bitrix24_read.")
        what = "не читает или не разрешён" if not write else "не разрешён (удалять через REST нельзя)"
        raise BxError("bad_method", f"Ошибка: этот метод {what}. Можно: " + ", ".join(sorted(allowed)))
    params = args.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    if not isinstance(params, dict):
        params = {}
    return method, params


def _compact(value, depth: int = 0):
    if isinstance(value, dict):
        return {k: _compact(v, depth + 1) for k, v in value.items() if v not in (None, "", [], {}) and depth < 6}
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value[:MAX_ROWS]]
    return value


def format_result(data: dict, write: bool = False) -> str:
    result = data.get("result")
    if write:
        if isinstance(result, dict) and isinstance(result.get("task"), dict):
            result = result["task"]  # tasks.task.add / update
        text = "Записано на учебном портале. Ответ: " + json.dumps(_compact(result), ensure_ascii=False)
        return (text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…") + (
            "\nПроверь запись через bitrix24_read (crm.*.get или tasks.task.get по ID) и сверь поля с задачей."
        )
    if isinstance(result, dict) and isinstance(result.get("tasks"), list):
        result = result["tasks"]  # tasks.task.list
    head = "Результат:"
    if isinstance(result, list):
        total = data.get("total", len(result))
        head = f"Найдено {total}, показано {min(len(result), MAX_ROWS)}."
        if data.get("next"):
            head += f" Дальше: params.start={data['next']}."
    text = f"{head}\n{json.dumps(_compact(result), ensure_ascii=False)}"
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…"


def fetch(base: str, method: str, params: dict, transport: httpx.BaseTransport | None = None) -> dict:
    from app.web import WebError, check_url

    url = base + method + ".json"
    try:
        if transport is None:
            check_url(url)
    except WebError:
        raise BxError("blocked_address", "Ошибка: адрес портала из «Доступов» недоступен.") from None
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.post(url, json=params)
    if response.status_code in (401, 403):
        raise BxError("auth_failed", "Ошибка: Битрикс24 не принял вебхук из «Доступов» (проверьте права вебхука: CRM, задачи, пользователи).")
    try:
        data = response.json()
    except ValueError:
        raise BxError("api_error", f"Ошибка REST Битрикс24 {response.status_code}.") from None
    if response.status_code >= 400 or "error" in data:
        detail = str(data.get("error_description") or data.get("error") or "")[:300]
        raise BxError("api_error", f"Ошибка REST Битрикс24 {response.status_code}: {detail}")
    return data


def webhook_for(session, user, portal: str | None) -> dict | None:
    from app.skills.hosts import host_matches, host_of
    from app.vault import list_credentials

    rows = [row for row in list_credentials(session, user) if is_webhook_login(row.get("username"))]
    target = host_of(portal)
    if target:
        for row in rows:
            site = host_of(row["site"])
            if site and (host_matches(target, site) or host_matches(site, target)):
                return row
        return None
    return rows[0] if rows else None


def execute(session, user, task_id, name: str, args: dict, transport: httpx.BaseTransport | None = None) -> ClientResult:
    """app.agent.executors contract."""
    from app.vault import VaultError, secret_for_browser

    write = name == "bitrix24_write"
    try:
        method, params = build_call(args, write)
        cred = webhook_for(session, user, str(args.get("portal") or "") or None)
        if cred is None:
            raise BxError(
                "no_credential",
                "Нет вебхука Битрикс24 в «Доступах». Работай через экран, а пользователю скажи: чтобы читать "
                "Битрикс24 без входа, создайте входящий вебхук (Разработчикам → Другое → Входящий вебхук) и "
                "сохраните его в «Доступах»: сайт — адрес портала, логин webhook, пароль — адрес вебхука.",
            )
        if write and not is_sandbox(cred["site"]):
            raise BxError(
                "write_forbidden",
                "Ошибка: портал " + str(cred["site"]) + " боевой, через REST на нём только чтение. Создай или измени "
                "запись на экране (опасные действия — после подтверждения пользователя), затем проверь её через bitrix24_read.",
            )
        try:
            secret = secret_for_browser(session, user, uuid.UUID(cred["id"]))
        except VaultError as exc:
            raise BxError(exc.code, "Ошибка: хранилище паролей недоступно.") from None
        data = fetch(webhook_base(cred["site"], secret), method, params, transport)
        if write:
            log.info("bitrix24 sandbox write %s", method)
        return ClientResult(format_result(data, write))
    except BxError as exc:
        return ClientResult(exc.text or f"Ошибка: {exc.code}", exc.code)
    except httpx.HTTPError as exc:
        log.info("bitrix24 api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: REST Битрикс24 не ответил.", "api_unavailable")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(TOOL)
    register_client_tool(WRITE_TOOL)
    register_executor(TOOL_NAMES, execute)
