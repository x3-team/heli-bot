"""Битрикс24 REST through the user's incoming webhook: deals, leads, contacts, tasks, users.

bitrix24_read reads on any portal: exact data for answers and a check of a record after «Сохранить» on the
screen. Without a login on the screen (the sign-in page asks a captcha from the server) read-only tasks can
be done through this tool alone.

bitrix24_write creates and changes records (deals, leads, contacts, companies, tasks, comments, stages).
On a training portal (SANDBOX_PORTALS plus HELI_BITRIX_SANDBOX_HOSTS) it writes at once. On a client's
portal it writes only after the user's «да» on the card the loop shows (app.agent.write_policy: portal,
method, fields); the call must name the portal, and without the approval nothing is sent. Nothing
deletes through REST, sandbox or not.

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

from app.agent import write_policy
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
    "Создать или изменить запись Битрикс24 через REST (вебхук в «Доступах»): сделка, лид, контакт, компания, "
    "задача, комментарий, смена стадии (crm.deal.update с params.id и fields.STAGE_ID: код стадии из воронки "
    "этой сделки или её название — сервер сверит его с воронкой и перечитает сделку). Всегда указывай portal. На учебном "
    "портале запись идёт сразу; на боевом портале клиента сервер сам покажет пользователю карточку "
    "подтверждения (портал, метод, поля) и запишет только после «да» — сам ask_confirmation не вызывай. Удаления нет. "
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


def classify(args: dict):
    """write_policy rule for bitrix24_write. The portal comes from the args, as the card shows it."""
    from app.skills.hosts import host_of

    method = str(args.get("method") or "").strip()
    host = host_of(str(args.get("portal") or "")) if args.get("portal") else ""
    if method not in WRITE_METHODS:
        return write_policy.read(host)  # the executor refuses it: nothing is written
    if host and is_sandbox(host):
        return write_policy.sandbox(host, method, method)
    params = args.get("params") or {}
    what = f"{method} — " + write_policy.short(params, 400)
    return write_policy.live(host or "портал не указан", what, verb=method)


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


def format_result(data: dict, write: bool = False, live: bool = False) -> str:
    result = data.get("result")
    if write:
        if isinstance(result, dict) and isinstance(result.get("task"), dict):
            result = result["task"]  # tasks.task.add / update
        where = "на боевом портале (подтверждено пользователем)" if live else "на учебном портале"
        text = f"Записано {where}. Ответ: " + json.dumps(_compact(result), ensure_ascii=False)
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


def _fields(params: dict) -> dict:
    for key, value in params.items():
        if str(key).lower() == "fields" and isinstance(value, dict):
            return value
    return {}


def _stage_key(params: dict) -> str:
    return next((k for k in _fields(params) if str(k).upper() == "STAGE_ID"), "")


def _deal_id(params: dict) -> str:
    return next((str(v).strip() for k, v in params.items() if str(k).lower() == "id" and str(v).strip()), "")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().strip("«»\"'").strip()).casefold()


def deal_stages(base: str, category: str, transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Stages of one pipeline: [{STATUS_ID, NAME}]. Pipeline 0 is DEAL_STAGE, pipeline N is DEAL_STAGE_N."""
    entity = "DEAL_STAGE" if category in ("", "0") else f"DEAL_STAGE_{category}"
    rows = fetch(base, "crm.status.list", {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}}, transport)
    stages = [r for r in (rows.get("result") or []) if isinstance(r, dict) and r.get("STATUS_ID")]
    if not stages and category not in ("", "0"):
        rows = fetch(base, "crm.dealcategory.stage.list", {"id": category}, transport)
        stages = [r for r in (rows.get("result") or []) if isinstance(r, dict) and r.get("STATUS_ID")]
    return stages


def resolve_stage(wanted, stages: list[dict], category: str) -> dict | None:
    """The pipeline's stage the model meant: its code, the code without the C<N>: prefix of the pipeline, or its name.

    Deals outside the main pipeline keep codes like C3:PREPARATION. A code from crm.status.list for DEAL_STAGE
    (pipeline 0) on such a deal is accepted by crm.deal.update with result true and changes nothing.
    """
    raw = str(wanted or "").strip()
    code = raw.upper()
    by_code = {str(s["STATUS_ID"]).upper(): s for s in stages}
    if code in by_code:
        return by_code[code]
    bare = re.sub(r"^C\d+:", "", code)
    if category not in ("", "0") and f"C{category}:{bare}" in by_code:
        return by_code[f"C{category}:{bare}"]
    if category in ("", "0") and bare in by_code:
        return by_code[bare]
    name = _norm(raw)
    for stage in stages:
        if name and _norm(stage.get("NAME")) == name:
            return stage
    return None


def _stage_label(stage_id: str, stages: list[dict]) -> str:
    for stage in stages:
        if str(stage.get("STATUS_ID")) == stage_id:
            return f"{stage_id} «{stage.get('NAME')}»"
    return stage_id or "—"


def update_deal_stage(base: str, params: dict, live: bool, transport: httpx.BaseTransport | None = None) -> str:
    """crm.deal.update with fields.STAGE_ID: the stage is checked against the deal's own pipeline and read back.

    Битрикс24 answers result: true to a stage from another pipeline and to a change a stage's required fields
    or a robot rolled back, so «Записано» alone is not proof. Raises BxError with the stages to pick from.
    """
    deal_id = _deal_id(params)
    if not deal_id:
        raise BxError("bad_params", "Ошибка: для смены стадии укажи params.id — ID сделки (crm.deal.list найдёт его).")
    deal = fetch(base, "crm.deal.get", {"id": deal_id}, transport).get("result") or {}
    category = str(deal.get("CATEGORY_ID") or "0")
    before = str(deal.get("STAGE_ID") or "")
    stages = deal_stages(base, category, transport)
    fields = _fields(params)
    key = _stage_key(params)
    wanted = fields.get(key)
    stage = resolve_stage(wanted, stages, category)
    listing = "; ".join(f"{s['STATUS_ID']} — {s.get('NAME', '')}" for s in stages) or "Битрикс24 не вернул список"
    if stage is None:
        raise BxError(
            "bad_stage",
            f"Не выполнено: в воронке сделки {deal_id} (CATEGORY_ID={category}) нет стадии «{wanted}». Ничего не "
            f"записано. Стадии этой воронки: {listing}. Повтори crm.deal.update с STAGE_ID из этого списка.",
        )
    target = str(stage["STATUS_ID"])
    if before == target:
        return f"Сделка {deal_id} уже на стадии {_stage_label(target, stages)}. Ничего не менялось."
    new_fields = {k: v for k, v in fields.items() if k != key}
    new_fields["STAGE_ID"] = target
    new_params = {k: v for k, v in params.items() if str(k).lower() != "fields"}
    new_params["fields"] = new_fields
    data = fetch(base, "crm.deal.update", new_params, transport)
    log.info("bitrix24 %s write crm.deal.update stage", "live" if live else "sandbox")
    after = str((fetch(base, "crm.deal.get", {"id": deal_id}, transport).get("result") or {}).get("STAGE_ID") or "")
    if after != target:
        raise BxError(
            "stage_not_changed",
            f"Битрикс24 принял crm.deal.update (ответ {json.dumps(data.get('result'), ensure_ascii=False)}), но стадия "
            f"сделки {deal_id} не сменилась: сейчас {_stage_label(after, stages)}, нужна {_stage_label(target, stages)}. "
            "Обычно мешают обязательные поля стадии или робот воронки. Прочитай сделку (crm.deal.get), заполни "
            "недостающие поля и повтори; не сообщай пользователю, что стадия сменилась.",
        )
    where = "на боевом портале (подтверждено пользователем)" if live else "на учебном портале"
    return (
        f"Записано {where}: стадия сделки {deal_id} {_stage_label(before, stages)} → {_stage_label(after, stages)}. "
        "Проверено чтением crm.deal.get."
    )


def webhook_for(session, user, portal: str | None) -> dict | None:
    from app.skills.hosts import host_matches, host_of
    from app.vault import list_credentials

    from app.agent.amocrm import account_host

    # amoCRM tokens may be saved with the same login «webhook»: they are not Битрикс24 webhooks.
    rows = [
        row
        for row in list_credentials(session, user)
        if is_webhook_login(row.get("username")) and not account_host(row.get("site"))
    ]
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
        live = write and not is_sandbox(cred["site"])
        if live and not (write_policy.confirmed(args) and str(args.get("portal") or "").strip()):
            # The card named the portal from the args; a live write without one, or without «да», never goes out.
            raise BxError(
                write_policy.REFUSED_CODE,
                "Не выполнено: портал " + str(cred["site"]) + " боевой, а запись в нём не подтверждена пользователем "
                "(или не указан portal). Ничего не отправлено. Вызови bitrix24_write с portal: сервер покажет "
                "пользователю карточку, и запись пойдёт после «да».",
            )
        try:
            secret = secret_for_browser(session, user, uuid.UUID(cred["id"]))
        except VaultError as exc:
            raise BxError(exc.code, "Ошибка: хранилище паролей недоступно.") from None
        base = webhook_base(cred["site"], secret)
        if write and method == "crm.deal.update" and _stage_key(params):
            return ClientResult(update_deal_stage(base, params, live, transport))
        data = fetch(base, method, params, transport)
        if write:
            log.info("bitrix24 %s write %s", "live" if live else "sandbox", method)
        return ClientResult(format_result(data, write, live))
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


write_policy.register("bitrix24_write", classify)
