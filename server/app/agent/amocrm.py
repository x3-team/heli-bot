"""amoCRM (и Kommo) REST API v4 with the user's long-lived token: leads, contacts, companies, pipelines,
tasks, notes, users and the incoming leads («Неразобранное»).

amocrm_read only sends GET to an allow-list of paths, on any account: exact data for answers and the check
of a record after it was written. With a token in «Доступах» this is the first way for any amoCRM task,
no sign-in on the screen (the sign-in can ask a captcha).

amocrm_write creates and changes records (contacts, companies, leads, stage of a lead, tasks, notes,
accepting an incoming lead). Allowed paths only: no DELETE, no «decline» of incoming leads, no changes of
pipelines, fields or users, at most MAX_WRITE_ITEMS records per call.
- On a training account (SANDBOX_HOSTS + HELI_AMO_SANDBOX_HOSTS) it writes at once.
- On any other account it writes only after the user's «да» on the card the loop shows
  (app.agent.write_policy: account, request, body), like mail_send. The call must name the account; the
  executor checks write_policy.confirmed(args) and that the confirmed account is the one it writes to.

Access: «Доступы» entry with site = the account (company.amocrm.ru, company.kommo.com), login
token / webhook / rest, password = the long-lived token (amoCRM → Настройки → Интеграции → своя интеграция →
«Ключи и доступы» → долгосрочный токен), «Bearer <токен>», or a URL with ?access_token=<токен>.
The call goes only to that account's host, never to a host taken from the secret. The token is never
shown in a result.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx

from app.agent import write_policy
from app.agent.control import ClientResult

log = logging.getLogger(__name__)

TIMEOUT_S = 20
RESULT_CHARS = 8000
MAX_ROWS = 50
MAX_LIMIT = 250
MAX_WRITE_ITEMS = 10
TOKEN_LOGINS = {"token", "токен", "webhook", "вебхук", "rest", "api"}
HOST_SUFFIXES = ("amocrm.ru", "amocrm.com", "kommo.com")
# Training accounts (our trial, test accounts). Everything else is a live account of a client.
SANDBOX_HOSTS: tuple[str, ...] = ()

_ENTITY = r"(?:leads|contacts|companies)"
_UID = r"[A-Za-z0-9_\-]{1,64}"
READ_PATHS = tuple(
    re.compile(p)
    for p in (
        r"account",
        rf"{_ENTITY}(?:/\d+)?",
        rf"{_ENTITY}/(?:\d+/)?notes(?:/\d+)?",
        rf"{_ENTITY}/\d+/links",
        rf"{_ENTITY}/custom_fields(?:/\d+)?",
        r"leads/pipelines(?:/\d+(?:/statuses(?:/\d+)?)?)?",
        r"leads/loss_reasons",
        rf"leads/unsorted(?:/summary|/{_UID})?",
        r"tasks(?:/\d+)?",
        r"users(?:/\d+)?",
        r"events",
    )
)
WRITE_PATHS = {
    "POST": tuple(
        re.compile(p)
        for p in (
            r"(?:leads|contacts|companies|tasks)",
            r"leads/complex",
            rf"{_ENTITY}/(?:\d+/)?notes",
            rf"{_ENTITY}/\d+/link",
            rf"leads/unsorted/{_UID}/accept",
        )
    ),
    "PATCH": tuple(re.compile(p) for p in (r"(?:leads|contacts|companies|tasks)(?:/\d+)?",)),
}
READ_KEYS = {"with", "page", "limit", "query", "filter", "order"}
READ_HELP = (
    "account; leads, leads/<id> (with=contacts); contacts, companies (query=строка поиска); "
    "leads/pipelines (этапы воронок); leads/unsorted (неразобранное); tasks; users; "
    "leads/<id>/notes, contacts/<id>/notes; <entity>/<id>/links; <entity>/custom_fields; events"
)
WRITE_HELP = (
    "POST contacts | companies | leads | leads/complex | tasks | leads/<id>/notes | leads/notes | "
    "leads/<id>/link | leads/unsorted/<uid>/accept; PATCH leads/<id> (status_id, pipeline_id, price, "
    "responsible_user_id) | contacts/<id> | companies/<id> | tasks/<id>"
)


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_ACCOUNT = {"type": "string", "description": "Адрес аккаунта, например company.amocrm.ru"}
_PATH = {"type": "string", "description": "Путь API v4, например /api/v4/leads или leads/pipelines"}

TOOL = _fn(
    "amocrm_read",
    "Прочитать amoCRM через REST API v4 (долгосрочный токен в «Доступах»), только GET: аккаунт, сделки, "
    "контакты, компании, воронки и этапы, задачи, неразобранное, пользователи, примечания. Если токен есть — "
    "это первый путь для любой задачи amoCRM, вход на экране не нужен. Проверяй им запись после amocrm_write. "
    "Пути: " + READ_HELP + ". Пример: path /api/v4/leads, params {\"query\": \"Ромашка\", \"with\": \"contacts\"}; "
    "фильтр по этапу: params {\"filter\": {\"statuses\": [{\"pipeline_id\": 1, \"status_id\": 2}]}}.",
    {
        "path": _PATH,
        "params": {
            "type": "object",
            "description": "Параметры запроса: with, page, limit (до 250), query, filter, order",
        },
        "account": _ACCOUNT,
    },
    ["path"],
)
WRITE_TOOL = _fn(
    "amocrm_write",
    "Создать или изменить запись amoCRM через REST API v4: контакт, компания, сделка, смена этапа "
    "(PATCH leads/<id> с status_id и pipeline_id из leads/pipelines), задача, примечание, принятие заявки из "
    "неразобранного. Всегда указывай account. На учебном аккаунте запись идёт сразу; на боевом аккаунте клиента "
    "сервер сам покажет пользователю карточку подтверждения (аккаунт, запрос, поля) и запишет только после «да» — "
    "сам ask_confirmation не вызывай. Удаления нет. Не больше 10 записей за вызов. После записи проверь её через amocrm_read по ID из ответа. "
    "Запросы: " + WRITE_HELP + ". Пример: method POST, path /api/v4/leads, body [{\"name\": \"…\", \"price\": "
    "62000, \"_embedded\": {\"contacts\": [{\"id\": 301}]}}]. Телефон и почта контакта — custom_fields_values "
    "с field_code PHONE / EMAIL. Срок задачи complete_till — unix-время.",
    {
        "method": {"type": "string", "enum": ["POST", "PATCH"]},
        "path": _PATH,
        "body": {
            "description": "Тело запроса: список записей (POST, массовый PATCH) или одна запись (PATCH по id)",
            "anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "object"}],
        },
        "account": _ACCOUNT,
    },
    ["method", "path", "body", "account"],
)
TOOL_NAMES = ("amocrm_read", "amocrm_write")

NO_TOKEN = (
    "Нет токена amoCRM в «Доступах». Передай пользователю: создайте в amoCRM своё приложение "
    "(Настройки → Интеграции → Создать интеграцию → внешняя), на вкладке «Ключи и доступы» сгенерируйте "
    "долгосрочный токен и сохраните его в Настройки → «Подключения» («Доступы»): сайт — адрес аккаунта "
    "(company.amocrm.ru), имя — token, секрет — токен. Пароль от amoCRM и токен в чат не пишите. "
    "Пока токена нет, данные можно смотреть на экране, если пользователь сам войдёт в аккаунт."
)


class AmoError(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.text = text


def is_token_login(username: str | None) -> bool:
    return str(username or "").strip().lower() in TOKEN_LOGINS


def sandbox_hosts() -> set[str]:
    return write_policy.hosts_from_env("HELI_AMO_SANDBOX_HOSTS", SANDBOX_HOSTS)


def account_host(site: str | None) -> str:
    """The account host from a «Доступы» site, or "" when it is not an amoCRM / Kommo account."""
    from app.skills.hosts import host_matches, host_of

    host = host_of((site or "").strip())
    if not host:
        return ""
    if host in sandbox_hosts():
        return host
    for suffix in HOST_SUFFIXES:
        if host_matches(host, suffix) and host != suffix and not host.startswith("www."):
            return host
    return ""


def is_sandbox(site: str) -> bool:
    host = account_host(site)
    return bool(host) and host in sandbox_hosts()


def token_of(secret: str) -> str:
    """The bare token from the saved secret: «<токен>», «Bearer <токен>» or a URL with access_token."""
    value = (secret or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if "://" in value:
        query = parse_qs(urlsplit(value).query)
        found = (query.get("access_token") or query.get("token") or [""])[0]
        if not found:
            raise AmoError(
                "bad_credential",
                "Ошибка: в секрете amoCRM в «Доступах» адрес без токена. Сохраните в секрет сам долгосрочный токен.",
            )
        value = found.strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-]{20,4096}", value):
        raise AmoError(
            "bad_credential",
            "Ошибка: секрет amoCRM в «Доступах» не похож на долгосрочный токен (длинная строка без пробелов).",
        )
    return value


def _path(raw) -> str:
    path = str(raw or "").strip()
    if "://" in path:
        path = urlsplit(path).path
    path = path.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    path = path.removeprefix("api/v4/").removeprefix("api/v4").strip("/")
    if ".." in path or "//" in path:
        return ""
    return path


def _flatten(prefix: str, value, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}[{key}]", item, out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _flatten(f"{prefix}[{index}]", item, out)
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    elif value is not None:
        out.append((prefix, str(value)))


def _query(params) -> list[tuple[str, str]]:
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    if not isinstance(params, dict):
        params = {}
    unknown = sorted(str(k) for k in params if k not in READ_KEYS)
    if unknown:
        raise AmoError(
            "bad_params",
            "Ошибка: параметры " + ", ".join(unknown) + " не поддерживаются. Можно: " + ", ".join(sorted(READ_KEYS)),
        )
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if key == "with" and isinstance(value, list):
            out.append(("with", ",".join(str(v) for v in value)))
        elif key == "limit":
            try:
                out.append(("limit", str(max(1, min(int(value), MAX_LIMIT)))))
            except (TypeError, ValueError):
                raise AmoError("bad_params", "Ошибка: limit — число до 250.") from None
        elif key in ("filter", "order") and isinstance(value, (dict, list)):
            _flatten(key, value, out)
        elif value is not None:
            out.append((key, str(value)))
    return out


def _body(raw):
    body = raw
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            raise AmoError("bad_body", "Ошибка: body — JSON: список записей или одна запись.") from None
    if isinstance(body, dict):
        return body
    if isinstance(body, list) and body and all(isinstance(item, dict) for item in body):
        if len(body) > MAX_WRITE_ITEMS:
            raise AmoError(
                "too_many",
                f"Ошибка: не больше {MAX_WRITE_ITEMS} записей за вызов. Массовые изменения — по частям и после "
                "согласования с пользователем.",
            )
        return body
    raise AmoError("bad_body", "Ошибка: body — список записей (объектов) или одна запись.")


def build_read(args: dict) -> tuple[str, list[tuple[str, str]]]:
    path = _path(args.get("path"))
    if str(args.get("method") or "GET").strip().upper() != "GET":
        raise AmoError("bad_method", "Ошибка: amocrm_read только читает (GET). Запись — через amocrm_write.")
    if not path or not any(p.fullmatch(path) for p in READ_PATHS):
        raise AmoError("bad_path", "Ошибка: этот путь не читается через amocrm_read. Можно: " + READ_HELP)
    return path, _query(args.get("params"))


def build_write(args: dict) -> tuple[str, str, object]:
    method = str(args.get("method") or "").strip().upper()
    path = _path(args.get("path"))
    if method == "GET":
        raise AmoError("bad_method", "Ошибка: чтение — через amocrm_read.")
    if method not in WRITE_PATHS:
        raise AmoError(
            "bad_method",
            "Ошибка: можно только POST и PATCH. Удалять через REST нельзя: удаление пользователь делает сам "
            "в amoCRM.",
        )
    if not path or not any(p.fullmatch(path) for p in WRITE_PATHS[method]):
        if "unsorted" in path and path.endswith("decline"):
            what = "Отклонение заявки из неразобранного удаляет её — так нельзя, это делает пользователь сам."
        elif "unlink" in path or "delete" in path:
            what = "Отвязку и удаление через REST не делаем."
        else:
            what = "Этот запрос не разрешён."
        raise AmoError("bad_path", f"Ошибка: {what} Можно: " + WRITE_HELP)
    body = _body(args.get("body"))
    if method == "PATCH" and re.fullmatch(r"\w+", path) and isinstance(body, list):
        if not all(isinstance(item.get("id"), (int, str)) and str(item.get("id")).isdigit() for item in body):
            raise AmoError("bad_body", "Ошибка: в массовом PATCH у каждой записи нужен id.")
    if method == "PATCH" and re.fullmatch(r"\w+", path) and isinstance(body, dict):
        raise AmoError("bad_body", "Ошибка: PATCH по одной записи — путь с id (leads/<id>), иначе body — список с id.")
    return method, path, body


def _compact(value, depth: int = 0):
    if isinstance(value, dict):
        return {
            k: _compact(v, depth + 1)
            for k, v in value.items()
            if k != "_links" and v not in (None, "", [], {}) and depth < 12
        }
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value[:MAX_ROWS]]
    return value


def _clip(text: str) -> str:
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…"


def format_read(data) -> str:
    if data is None:
        return "Ничего не найдено (amoCRM ответил пустым списком)."
    embedded = data.get("_embedded") if isinstance(data, dict) else None
    head = "Результат:"
    if isinstance(embedded, dict):
        rows = {k: v for k, v in embedded.items() if isinstance(v, list)}
        if rows:
            head = "Найдено: " + ", ".join(f"{k} {len(v)}" for k, v in rows.items())
            if any(len(v) > MAX_ROWS for v in rows.values()):
                head += f" (показано до {MAX_ROWS})"
            if isinstance(data.get("_page"), int) and isinstance(data.get("_links"), dict) and data["_links"].get("next"):
                head += f". Дальше: params.page={data['_page'] + 1}"
            head += "."
    return _clip(f"{head}\n{json.dumps(_compact(data), ensure_ascii=False)}")


def format_write(data, sandbox: bool) -> str:
    where = "на учебном аккаунте" if sandbox else "на боевом аккаунте (с подтверждения пользователя)"
    text = f"Записано в amoCRM {where}. Ответ: " + json.dumps(_compact(data), ensure_ascii=False)
    return _clip(text) + "\nПроверь запись через amocrm_read (GET по ID из ответа) и сверь поля с задачей."


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [str(data.get(k)) for k in ("title", "detail") if data.get(k)]
    problems = data.get("validation-errors") or (data.get("_embedded") or {}).get("errors")
    if problems:
        parts.append(json.dumps(problems, ensure_ascii=False))
    return " ".join(parts)[:400]


def fetch(
    host: str,
    token: str,
    method: str,
    path: str,
    *,
    query: list[tuple[str, str]] | None = None,
    body=None,
    transport: httpx.BaseTransport | None = None,
):
    from app.web import WebError, check_url

    url = f"https://{host}/api/v4/{path}"
    try:
        if transport is None:
            check_url(url)
    except WebError:
        raise AmoError("blocked_address", "Ошибка: адрес аккаунта amoCRM из «Доступов» недоступен.") from None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.request(method, url, params=query or None, json=body, headers=headers)
    status = response.status_code
    if status == 204:
        return None
    if status == 401:
        raise AmoError(
            "auth_failed",
            "Ошибка: amoCRM не принял токен из «Доступов» (истёк, отозван или выдан для другого аккаунта). "
            "Пусть пользователь создаст новый долгосрочный токен и заменит его в «Доступах».",
        )
    if status == 402:
        raise AmoError("payment_required", "Ошибка: аккаунт amoCRM не оплачен или пробный период закончился.")
    if status == 403:
        raise AmoError("forbidden", "Ошибка: у токена amoCRM нет прав на это действие (или аккаунт заблокирован).")
    if status == 404:
        raise AmoError("not_found", "Ошибка: amoCRM ответил 404 — такой записи или пути нет.")
    if status == 429:
        raise AmoError("rate_limited", "Ошибка: amoCRM ограничил частоту запросов. Подожди и сделай меньше запросов.")
    if status >= 300:
        detail = _error_text(response).replace(token, "<CREDENTIAL>")
        raise AmoError("api_error", f"Ошибка API amoCRM {status}: {detail}".strip())
    try:
        return response.json()
    except ValueError:
        raise AmoError("api_error", f"Ошибка API amoCRM {status}: ответ не JSON.") from None


def call(
    host: str,
    token: str,
    name: str,
    args: dict,
    *,
    sandbox: bool,
    confirmed: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> ClientResult:
    """One amocrm_read / amocrm_write call against a known account. The token never reaches the result."""
    write = name == "amocrm_write"
    try:
        if write:
            method, path, body = build_write(args)
            if not sandbox and not (confirmed and account_host(args.get("account")) == host):
                # The card named the account from the args: a live write without «да», or to another account
                # than the one confirmed, never goes out.
                raise AmoError(
                    write_policy.REFUSED_CODE,
                    f"Не выполнено: аккаунт {host} боевой, а запись в нём не подтверждена пользователем (или в "
                    f"account не указан {host}). Ничего не отправлено. Вызови amocrm_write с account {host}: "
                    "сервер покажет пользователю карточку, и запись пойдёт после «да». При отказе ничего не меняй.",
                )
            data = fetch(host, token, method, path, body=body, transport=transport)
            log.info("amocrm %s write %s %s", "sandbox" if sandbox else "live", method, path)
            return ClientResult(format_write(data, sandbox))
        path, query = build_read(args)
        return ClientResult(format_read(fetch(host, token, "GET", path, query=query, transport=transport)))
    except AmoError as exc:
        return ClientResult((exc.text or f"Ошибка: {exc.code}").replace(token, "<CREDENTIAL>"), exc.code)
    except httpx.HTTPError as exc:
        log.info("amocrm api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: API amoCRM не ответил.", "api_unavailable")


def token_entry_for(session, user, account: str | None) -> dict | None:
    """The «Доступы» row with an amoCRM token: for the given account, else the first amoCRM one."""
    from app.skills.hosts import host_matches
    from app.vault import list_credentials

    rows = [
        row
        for row in list_credentials(session, user)
        if is_token_login(row.get("username")) and account_host(row.get("site"))
    ]
    target = account_host(account) or str(account or "").strip().lower()
    if target:
        for row in rows:
            site = account_host(row["site"])
            if host_matches(site, target) or site.split(".", 1)[0] == target:
                return row
        return None
    return rows[0] if rows else None


def execute(session, user, task_id, name: str, args: dict, transport: httpx.BaseTransport | None = None) -> ClientResult:
    """app.agent.executors contract."""
    from app.vault import VaultError, secret_for_browser

    cred = token_entry_for(session, user, str(args.get("account") or "") or None)
    if cred is None:
        return ClientResult(NO_TOKEN, "no_credential")
    host = account_host(cred["site"])
    try:
        secret = secret_for_browser(session, user, uuid.UUID(cred["id"]))
    except VaultError as exc:
        return ClientResult("Ошибка: хранилище паролей недоступно.", exc.code)
    try:
        token = token_of(secret)
    except AmoError as exc:
        return ClientResult(exc.text, exc.code)
    return call(
        host,
        token,
        name,
        args,
        sandbox=is_sandbox(host),
        confirmed=write_policy.confirmed(args),
        transport=transport,
    )


def classify(args: dict) -> write_policy.Write:
    """write_policy rule for amocrm_write, from the args only: the account named in the call."""
    host = account_host(args.get("account"))
    method = str(args.get("method") or "").strip().upper()
    path = _path(args.get("path"))
    try:
        build_write(args)
    except AmoError:
        return write_policy.read(host)  # the executor refuses it: nothing is written
    verb = f"{method} {path}"
    if host and host in sandbox_hosts():
        return write_policy.sandbox(host, verb, verb)
    what = f"{verb} — " + write_policy.short(args.get("body"), 400)
    return write_policy.live(host or "аккаунт amoCRM не указан", what, verb=verb)


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(TOOL)
    register_client_tool(WRITE_TOOL)
    register_executor(TOOL_NAMES, execute)


write_policy.register("amocrm_write", classify)
