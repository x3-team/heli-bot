"""Google Analytics 4 for the agent: reports are read on any property, test events go only to a training one.

ga4_read: Data API reports (runReport, runRealtimeReport, metadata) and Admin API lists (account summaries,
key events, data streams). Data API reports are POST by protocol but only read; everything else is GET.
Nothing here changes a property's settings: the Admin API is called with GET only and the service-account
token asks for the analytics.readonly scope.

ga4_send_event: a Measurement Protocol hit (for example heli_train_ping), and only to a training Measurement ID:
- HELI_GA4_SANDBOX_IDS (comma-separated, set by the server owner), or
- a «Доступы» entry with site «ga4-sandbox», login = the Measurement ID, password = its API secret
  (the user adds it in the app; the agent's save_credential cannot create such an entry).
Any other ID — the getheli site, a client's site, a G-XXXX from a page or a task — is refused before a
credential is read and before any network call. There is no switch that opens Measurement Protocol to a live
property. The live getheli and client IDs are never put into this list or into the repository.

Access for reading: a «Доступы» entry with site analytics.google.com (or analyticsdata.googleapis.com), login
token (password = OAuth access token with analytics.readonly) or login service_account (password = the
service-account JSON key; the server signs a JWT and gets a read-only token). Secrets never reach the model.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

import httpx

from app.agent.control import ClientResult

log = logging.getLogger(__name__)

DATA_API = "https://analyticsdata.googleapis.com/v1beta/"
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta/"
MP_URL = "https://www.google-analytics.com/mp/collect"
MP_DEBUG_URL = "https://www.google-analytics.com/debug/mp/collect"
TOKEN_URL = "https://oauth2.googleapis.com/token"
READ_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
TIMEOUT_S = 30
RESULT_CHARS = 8000

SANDBOX_SITE = "ga4-sandbox"
READ_SITES = ("analytics.google.com", "analyticsdata.googleapis.com", "analyticsadmin.googleapis.com", "googleapis.com")
TOKEN_LOGINS = {"token", "oauth", "oauth_token", "bearer"}
SA_LOGINS = {"service_account", "service-account", "serviceaccount", "json"}

MEASUREMENT_ID_RE = re.compile(r"^G-[A-Z0-9]{4,16}$")
EVENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
RESERVED_PREFIXES = ("google_", "ga_", "firebase_")
MAX_EVENTS = 25

# report -> (api, method, path); {p} is the property number.
REPORTS = {
    "run_report": ("data", "POST", "properties/{p}:runReport"),
    "realtime_report": ("data", "POST", "properties/{p}:runRealtimeReport"),
    "metadata": ("data", "GET", "properties/{p}/metadata"),
    "key_events": ("admin", "GET", "properties/{p}/keyEvents"),
    "data_streams": ("admin", "GET", "properties/{p}/dataStreams"),
    "account_summaries": ("admin", "GET", "accountSummaries"),
}


class GaError(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.text = text or f"Ошибка: {code}"


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


READ_TOOL = _fn(
    "ga4_read",
    "Google Analytics 4, только чтение (ключ из «Доступов»). report: run_report — отчёт Data API (params: "
    "dateRanges, dimensions, metrics, dimensionFilter, limit; метрики eventCount, totalUsers, activeUsers, "
    "keyEvents, sessions; измерения eventName, date, sessionSource), realtime_report, metadata (какие измерения "
    "и метрики есть), key_events (какие события отмечены ключевыми), data_streams (потоки и их Measurement ID), "
    "account_summaries (ресурсы). Настройки ресурса не меняет. Пример: report run_report, property 123456, "
    "params {\"dateRanges\": [{\"startDate\": \"7daysAgo\", \"endDate\": \"today\"}], \"dimensions\": "
    "[{\"name\": \"eventName\"}], \"metrics\": [{\"name\": \"eventCount\"}, {\"name\": \"keyEvents\"}]}.",
    {
        "report": {"type": "string", "enum": sorted(REPORTS)},
        "property": {"type": "string", "description": "Номер ресурса GA4 (property id), например 123456789"},
        "params": {"type": "object", "description": "Тело запроса runReport / runRealtimeReport"},
    },
    ["report"],
)
SEND_TOOL = _fn(
    "ga4_send_event",
    "Отправить тестовое событие Measurement Protocol в GA4 — только на учебный Measurement ID (список учебных "
    "ресурсов сервера или запись «ga4-sandbox» в «Доступах»). Боевой сайт и сайты клиентов — отказ без "
    "отправки: проверять их хитами нельзя, только читать отчёты (ga4_read). validate=true — проверка формата "
    "на отладочном адресе без записи. Пример: measurement_id G-…, events [{\"name\": \"heli_train_ping\", "
    "\"params\": {\"source\": \"heli\"}}].",
    {
        "measurement_id": {"type": "string", "description": "Учебный Measurement ID вида G-XXXXXXXX"},
        "events": {
            "type": "array",
            "description": "События: [{\"name\": \"heli_train_ping\", \"params\": {…}}], до 25",
            "items": {"type": "object"},
        },
        "client_id": {"type": "string", "description": "client_id; по умолчанию учебный heli-train.*"},
        "validate": {"type": "boolean", "description": "true — только проверка формата (debug/mp/collect)"},
    },
    ["measurement_id", "events"],
)
TOOL_NAMES = ("ga4_read", "ga4_send_event")


# --- the training list -------------------------------------------------------------


def normalize_id(value) -> str:
    return str(value or "").strip().upper()


def env_sandbox_ids() -> set[str]:
    raw = os.environ.get("HELI_GA4_SANDBOX_IDS", "")
    return {normalize_id(part) for part in raw.split(",") if MEASUREMENT_ID_RE.match(normalize_id(part))}


def _is_sandbox_site(site: str) -> bool:
    return str(site or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/") == SANDBOX_SITE


def vault_sandbox_ids(rows: list[dict]) -> set[str]:
    return {normalize_id(r["username"]) for r in rows if _is_sandbox_site(r["site"]) and MEASUREMENT_ID_RE.match(normalize_id(r["username"]))}


def check_send(args: dict, sandbox_ids: set[str]) -> tuple[str, list[dict], str, bool]:
    """(measurement_id, events, client_id, validate). Raises before anything is read or sent."""
    mid = normalize_id(args.get("measurement_id"))
    if not MEASUREMENT_ID_RE.match(mid):
        raise GaError("bad_measurement_id", "Ошибка: Measurement ID GA4 выглядит как G-XXXXXXXX.")
    if mid not in sandbox_ids:
        raise GaError(
            "write_forbidden",
            f"Ошибка: {mid} не учебный ресурс. Хиты Measurement Protocol и изменения настроек — только на учебный "
            "Measurement ID. На боевой сайт и сайты клиентов тестовые хиты не отправляются даже «для проверки»: "
            "они портят статистику и ключевые события. Проверь данные чтением (ga4_read: run_report, key_events) "
            "или в DebugView учебного ресурса; если нужен учебный ресурс — попроси пользователя добавить его в "
            "«Доступы» (сайт ga4-sandbox, логин — Measurement ID, пароль — API secret).",
        )
    events = args.get("events")
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except ValueError:
            events = None
    if isinstance(events, dict):
        events = [events]
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise GaError("bad_events", f"Ошибка: events — список от 1 до {MAX_EVENTS} событий с полем name.")
    clean = []
    for event in events:
        name = str((event or {}).get("name") or "") if isinstance(event, dict) else ""
        if not EVENT_NAME_RE.match(name) or name.lower().startswith(RESERVED_PREFIXES):
            raise GaError(
                "bad_events",
                "Ошибка: имя события — латиница, цифры и _, до 40 символов, с буквы, без префиксов google_, ga_, firebase_.",
            )
        params = event.get("params") or {}
        clean.append({"name": name, "params": params if isinstance(params, dict) else {}})
    client_id = str(args.get("client_id") or "").strip() or f"heli-train.{uuid.uuid4().hex[:12]}"
    return mid, clean, client_id, bool(args.get("validate"))


# --- reading -----------------------------------------------------------------------


def build_read(args: dict) -> tuple[str, str, dict | None]:
    """(method, url, body). Only the reads in REPORTS exist."""
    report = str(args.get("report") or "")
    if report not in REPORTS:
        raise GaError("bad_report", "Ошибка: можно " + ", ".join(sorted(REPORTS)))
    api, method, path = REPORTS[report]
    if "{p}" in path:
        prop = str(args.get("property") or "").strip().removeprefix("properties/")
        if not prop.isdigit():
            raise GaError("bad_property", "Ошибка: укажи номер ресурса GA4 (property), только цифры. Найти: account_summaries.")
        path = path.format(p=prop)
    body = None
    if method == "POST":
        params = args.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                params = {}
        body = params if isinstance(params, dict) else {}
        if report == "run_report":
            body.setdefault("dateRanges", [{"startDate": "28daysAgo", "endDate": "today"}])
        body.setdefault("limit", 100)
    return method, (DATA_API if api == "data" else ADMIN_API) + path, body


_SA_CACHE: dict[str, tuple[str, float]] = {}


def service_account_token(key_json: str, transport: httpx.BaseTransport | None = None) -> str:
    """A read-only access token for a service-account key (JWT bearer grant)."""
    import jwt

    try:
        key = json.loads(key_json)
        email, private_key = key["client_email"], key["private_key"]
    except (ValueError, KeyError, TypeError):
        raise GaError("bad_credential", "Ошибка: в «Доступах» для GA4 ожидается JSON-ключ сервисного аккаунта.") from None
    cached = _SA_CACHE.get(email)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": email, "scope": READ_SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600},
        private_key, algorithm="RS256",
    )
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if response.status_code != 200:
        raise GaError("auth_failed", "Ошибка: Google не выдал токен по ключу сервисного аккаунта.")
    token = str(response.json().get("access_token") or "")
    _SA_CACHE[email] = (token, time.time() + float(response.json().get("expires_in", 3600)))
    return token


def call_read(method: str, url: str, body: dict | None, token: str, transport: httpx.BaseTransport | None = None) -> str:
    if method not in {"GET", "POST"} or (method == "POST" and not url.startswith(DATA_API)):
        raise GaError("write_forbidden", "Ошибка: ga4_read только читает.")  # guard against a future REPORTS edit
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.request(method, url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if response.status_code in (401, 403):
        raise GaError("auth_failed", "Ошибка: Google Analytics не принял ключ из «Доступов» (нужен доступ к ресурсу с правом чтения).")
    try:
        data = response.json()
    except ValueError:
        raise GaError("api_error", f"Ошибка Google Analytics {response.status_code}.") from None
    if response.status_code >= 400:
        message = str((data.get("error") or {}).get("message", ""))[:300] if isinstance(data, dict) else ""
        raise GaError("api_error", f"Ошибка Google Analytics {response.status_code}: {message}")
    text = json.dumps(data, ensure_ascii=False)
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…"


# --- sending (training only) -------------------------------------------------------


def call_send(mid: str, api_secret: str, events: list[dict], client_id: str, validate: bool,
              transport: httpx.BaseTransport | None = None) -> str:
    url = MP_DEBUG_URL if validate else MP_URL
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.post(url, params={"measurement_id": mid, "api_secret": api_secret},
                               json={"client_id": client_id, "events": events})
    names = ", ".join(e["name"] for e in events)
    if validate:
        try:
            messages = response.json().get("validationMessages", [])
        except ValueError:
            messages = []
        if messages:
            return "Проверка формата (без записи): ошибки " + json.dumps(messages, ensure_ascii=False)[:2000]
        return f"Проверка формата (без записи): событие {names} для {mid} корректно."
    if response.status_code >= 300:
        raise GaError("api_error", f"Ошибка Measurement Protocol {response.status_code}.")
    return (
        f"Учебный ресурс {mid}: отправлено {names} (client_id {client_id}). Measurement Protocol не сообщает об "
        "ошибках в данных — проверь событие в отчёте реального времени (ga4_read realtime_report) или DebugView."
    )


# --- «Доступы» -----------------------------------------------------------------------


def _rows(session, user) -> list[dict]:
    from app.vault import list_credentials

    return list_credentials(session, user)


def _secret(session, user, row: dict) -> str:
    from app.vault import VaultError, secret_for_browser

    try:
        return secret_for_browser(session, user, uuid.UUID(row["id"]))
    except VaultError:
        raise GaError("vault_unavailable", "Ошибка: хранилище ключей недоступно.") from None


def read_token(session, user, transport: httpx.BaseTransport | None = None) -> str:
    from app.skills.hosts import host_matches, host_of

    for wanted in READ_SITES:
        for row in _rows(session, user):
            login = row["username"].strip().lower()
            site = host_of(row["site"])
            if login not in TOKEN_LOGINS | SA_LOGINS or not site or not (site == wanted or host_matches(site, wanted)):
                continue
            secret = _secret(session, user, row)
            if login in SA_LOGINS or secret.lstrip().startswith("{"):
                return service_account_token(secret, transport)
            return secret
    raise GaError(
        "no_credential",
        "Нет ключа Google Analytics в «Доступах». Пользователю: дайте сервисному аккаунту роль «Читатель» в ресурсе "
        "GA4 и сохраните JSON-ключ в «Доступах»: сайт analytics.google.com, логин service_account, пароль — "
        "содержимое ключа. Пока его нет, отчёты можно смотреть на экране analytics.google.com.",
    )


def api_secret_for(session, user, mid: str) -> str:
    from app.skills.hosts import host_matches, host_of

    for row in _rows(session, user):
        if normalize_id(row["username"]) != mid:
            continue
        site = host_of(row["site"]) or ""
        if _is_sandbox_site(row["site"]) or host_matches(site, "google-analytics.com") or host_matches(site, "analytics.google.com"):
            return _secret(session, user, row)
    raise GaError(
        "no_credential",
        f"Нет API secret для учебного {mid}. Пользователю: в GA4 → Администратор → Потоки данных → Measurement "
        "Protocol API secrets создайте секрет и сохраните в «Доступах»: сайт ga4-sandbox, логин — Measurement ID, "
        "пароль — секрет.",
    )


def execute(session, user, task_id, name: str, args: dict, transport: httpx.BaseTransport | None = None) -> ClientResult:
    """app.agent.executors contract."""
    secrets: list[str] = []

    def clean(text: str) -> str:
        for secret in secrets:
            if secret and len(secret) >= 4:
                text = text.replace(secret, "<CREDENTIAL>")
        return text

    try:
        if name == "ga4_send_event":
            sandbox = env_sandbox_ids() | vault_sandbox_ids(_rows(session, user))
            mid, events, client_id, validate = check_send(args, sandbox)
            api_secret = api_secret_for(session, user, mid)
            secrets.append(api_secret)
            log.info("ga4 training hit %s events=%d validate=%s", mid, len(events), validate)
            return ClientResult(clean(call_send(mid, api_secret, events, client_id, validate, transport)))
        method, url, body = build_read(args)
        token = read_token(session, user, transport)
        secrets.append(token)
        return ClientResult(clean(call_read(method, url, body, token, transport)))
    except GaError as exc:
        return ClientResult(clean(exc.text), exc.code)
    except httpx.HTTPError as exc:
        log.info("ga4 api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: Google Analytics не ответил.", "api_unavailable")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(READ_TOOL)
    register_client_tool(SEND_TOOL)
    register_executor(TOOL_NAMES, execute)
