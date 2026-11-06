"""Яндекс Директ API v5 and Метрика for the agent.

direct_api: campaigns, adgroups, ads, keywords, bids, keywordbids, dictionaries, clients, agencyclients
and reports. A live account is read only: method «get» (and Reports, which only reads). Any other method
(add, update, delete, suspend, resume, archive, set…) runs only in the sandbox
api-sandbox.direct.yandex.com (sandbox=true). Reports are TSV: the API answers 201/202 while it builds
one, and the tool waits retryIn seconds and asks again.

metrika_read: GET-only reads of Метрика (stat/v1/data, bytime, counters, goals).

Access: «Доступы» entry with site direct.yandex.ru (or api.direct.yandex.com, oauth.yandex.ru, yandex.ru),
login «token» and the OAuth token as the password. Метрика uses an entry for metrika.yandex.ru, and the
Директ token when there is none (one Яндекс OAuth app can have both scopes). The token goes only to the
Яндекс API hosts below and never reaches the model.

The cloud browser keeps the user's Яндекс sign-in (cookies per account) for the web interface; a captcha
there is never solved by the agent: the run pauses and the user passes it on the «Компьютер агента» screen.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import httpx

from app.agent.control import ClientResult

log = logging.getLogger(__name__)

LIVE = "https://api.direct.yandex.com/json/v5/"
SANDBOX = "https://api-sandbox.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/"
TIMEOUT_S = 30
RESULT_CHARS = 8000
REPORT_WAIT_S = 90
REPORT_MAX_TRIES = 12
TSV_ROWS = 200

SERVICES = (
    "campaigns", "adgroups", "ads", "keywords", "bids", "keywordbids", "bidmodifiers", "audiencetargets",
    "retargetinglists", "sitelinks", "vcards", "adextensions", "negativekeywordsharedsets", "dictionaries",
    "clients", "agencyclients", "changes", "keywordsresearch", "leads", "strategies", "reports",
)
READ_METHODS = {"get", "checkCampaigns", "checkDictionaries", "check", "hasSearchVolume", "deduplicate"}
TOKEN_SITES = ("direct.yandex.ru", "direct.yandex.com", "api.direct.yandex.com", "oauth.yandex.ru", "yandex.ru")
METRIKA_SITES = ("metrika.yandex.ru", "api-metrika.yandex.net")
TOKEN_LOGINS = {"token", "oauth", "oauth_token", "bearer"}

METRIKA_PATHS = {
    "stat": "stat/v1/data",
    "bytime": "stat/v1/data/bytime",
    "drilldown": "stat/v1/data/drilldown",
    "counters": "management/v1/counters",
    "goals": "management/v1/counter/{counter}/goals",
    "counter": "management/v1/counter/{counter}",
}


class DirectError(Exception):
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


DIRECT_TOOL = _fn(
    "direct_api",
    "Яндекс Директ API v5 под OAuth-токеном пользователя из «Доступов». В боевом аккаунте только чтение: "
    "method get у campaigns, adgroups, ads, keywords, bids, dictionaries и др., и отчёты (service reports, "
    "TSV; сервер сам дождётся готовности). Методы add, update, delete, suspend, resume, set — только с "
    "sandbox=true (песочница api-sandbox.direct.yandex.com). Пример: service campaigns, method get, params "
    "{\"SelectionCriteria\": {}, \"FieldNames\": [\"Id\", \"Name\", \"State\", \"Statistics\"]}. Отчёт: service "
    "reports, params {\"SelectionCriteria\": {\"DateFrom\": \"2026-10-01\", \"DateTo\": \"2026-10-31\"}, "
    "\"FieldNames\": [\"CampaignName\", \"Impressions\", \"Clicks\", \"Cost\", \"Conversions\"], \"ReportName\": "
    "\"…\", \"ReportType\": \"CAMPAIGN_PERFORMANCE_REPORT\", \"DateRangeType\": \"CUSTOM_DATE\", \"Format\": "
    "\"TSV\", \"IncludeVAT\": \"YES\"}.",
    {
        "service": {"type": "string", "enum": list(SERVICES)},
        "method": {"type": "string", "description": "get (бой и песочница) или add/update/… (только песочница)"},
        "params": {"type": "object", "description": "params запроса API v5; для reports — тело params отчёта"},
        "client_login": {"type": "string", "description": "Логин клиента для агентского аккаунта (заголовок Client-Login)"},
        "goals": {"type": "array", "items": {"type": "integer"}, "description": "Для reports: id целей Метрики для Conversions"},
        "sandbox": {"type": "boolean", "description": "true — песочница, там можно менять"},
    },
    ["service"],
)
METRIKA_TOOL = _fn(
    "metrika_read",
    "Яндекс Метрика, только чтение: stat (отчёт stat/v1/data: ids, metrics, dimensions, date1, date2, filters), "
    "bytime, drilldown, counters (список счётчиков), goals (цели счётчика), counter. Конверсии по целям: "
    "metrics ym:s:goal<ID>reaches, ym:s:goal<ID>conversionRate.",
    {
        "report": {"type": "string", "enum": sorted(METRIKA_PATHS)},
        "counter": {"type": "integer", "description": "Номер счётчика для goals и counter"},
        "params": {"type": "object", "description": "Параметры запроса, например {\"ids\": 123, \"metrics\": \"ym:s:visits\", \"date1\": \"7daysAgo\"}"},
    },
    ["report"],
)
TOOL_NAMES = ("direct_api", "metrika_read")


# --- building requests (no network) ------------------------------------------------


def build_direct(args: dict) -> tuple[str, dict, dict, bool]:
    """(url, headers without auth, body, is_report). Refuses a write outside the sandbox."""
    service = str(args.get("service") or "").strip().lower()
    if service not in SERVICES:
        raise DirectError("bad_service", "Ошибка: такого сервиса API v5 нет. Можно: " + ", ".join(SERVICES))
    sandbox = bool(args.get("sandbox"))
    params = args.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    if not isinstance(params, dict):
        params = {}
    headers = {"Accept-Language": "ru"}
    login = str(args.get("client_login") or "").strip()
    if login:
        headers["Client-Login"] = login
    base = SANDBOX if sandbox else LIVE
    if service == "reports":
        goals = args.get("goals")
        if isinstance(goals, list) and goals:
            params = {**params, "Goals": [int(g) for g in goals if str(g).isdigit()]}
        params.setdefault("Format", "TSV")
        params.setdefault("IncludeVAT", "YES")
        headers.update({
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipReportSummary": "false",
        })
        return base + "reports", headers, {"params": params}, True
    method = str(args.get("method") or "get").strip()
    if method not in READ_METHODS and not sandbox:
        raise DirectError(
            "write_forbidden",
            f"Ошибка: {service}.{method} меняет боевой аккаунт Директа, а он только для чтения. "
            "Проверь изменение в песочнице (sandbox=true) и опиши пользователю, что поменять в кабинете.",
        )
    return base + service, headers, {"method": method, "params": params}, False


def build_metrika(args: dict) -> tuple[str, dict]:
    report = str(args.get("report") or "")
    if report not in METRIKA_PATHS:
        raise DirectError("bad_report", "Ошибка: можно " + ", ".join(sorted(METRIKA_PATHS)))
    path = METRIKA_PATHS[report]
    if "{counter}" in path:
        counter = str(args.get("counter") or "")
        if not counter.isdigit():
            raise DirectError("bad_counter", "Ошибка: укажи номер счётчика (counter).")
        path = path.format(counter=counter)
    params = args.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    params = {str(k): (",".join(map(str, v)) if isinstance(v, list) else v) for k, v in params.items()}
    if report in {"stat", "bytime", "drilldown"}:
        params.setdefault("limit", 100)
    return METRIKA + path, params


# --- results -----------------------------------------------------------------------


def format_tsv(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "Отчёт пустой: за период нет данных."
    head = f"Отчёт TSV, строк {max(len(lines) - 1, 0)}" + (f", показано {TSV_ROWS}" if len(lines) - 1 > TSV_ROWS else "") + ":"
    body = "\n".join(lines[: TSV_ROWS + 1] + ([lines[-1]] if len(lines) > TSV_ROWS + 1 else []))
    out = f"{head}\n{body}"
    return out if len(out) <= RESULT_CHARS else out[:RESULT_CHARS] + "\n…[отчёт обрезан]"


def format_json(data: dict, units: str | None = None) -> str:
    if "error" in data:
        err = data["error"]
        raise DirectError(
            "api_error",
            f"Ошибка API Директа {err.get('error_code')}: {err.get('error_string')}. {err.get('error_detail', '')}"[:600],
        )
    text = json.dumps(data.get("result", data), ensure_ascii=False)
    if units:
        text = f"Баллы (потрачено/осталось/лимит): {units}\n{text}"
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…"


# --- network -----------------------------------------------------------------------


def call_direct(url: str, headers: dict, body: dict, is_report: bool, token: str,
                transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> str:
    auth = {"Authorization": f"Bearer {token}", **headers}
    waited = 0.0
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        for _ in range(REPORT_MAX_TRIES if is_report else 1):
            response = client.post(url, headers=auth, json=body)
            if response.status_code in (401, 403):
                raise DirectError("auth_failed", "Ошибка: Директ не принял токен из «Доступов» (нужен OAuth-токен с доступом к API Директа).")
            if is_report and response.status_code in (201, 202):
                try:
                    pause = float(response.headers.get("retryIn", "5"))
                except ValueError:
                    pause = 5.0
                pause = max(1.0, min(pause, 30.0))
                if waited + pause > REPORT_WAIT_S:
                    break
                waited += pause
                sleep(pause)
                continue
            if is_report and response.status_code == 200:
                return format_tsv(response.text)
            if is_report:
                try:
                    return format_json(response.json())
                except ValueError:
                    raise DirectError("api_error", f"Ошибка отчёта Директа {response.status_code}.") from None
            try:
                data = response.json()
            except ValueError:
                raise DirectError("api_error", f"Ошибка API Директа {response.status_code}.") from None
            return format_json(data, response.headers.get("Units"))
    raise DirectError(
        "report_pending",
        f"Отчёт ещё строится (ждал {int(waited)} с). Вызови тот же отчёт с тем же ReportName через минуту: "
        "Директ отдаст готовый.",
    )


def call_metrika(url: str, params: dict, token: str, transport: httpx.BaseTransport | None = None) -> str:
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.get(url, params=params, headers={"Authorization": f"OAuth {token}"})
    if response.status_code in (401, 403):
        raise DirectError("auth_failed", "Ошибка: Метрика не приняла токен (нужен доступ metrika:read).")
    try:
        data = response.json()
    except ValueError:
        raise DirectError("api_error", f"Ошибка Метрики {response.status_code}.") from None
    if response.status_code >= 400:
        raise DirectError("api_error", f"Ошибка Метрики {response.status_code}: {str(data.get('message', ''))[:300]}")
    text = json.dumps(data, ensure_ascii=False)
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…"


def token_for(session, user, sites: tuple[str, ...]) -> str | None:
    from app.skills.hosts import host_matches, host_of
    from app.vault import VaultError, list_credentials, secret_for_browser

    rows = [r for r in list_credentials(session, user) if r["username"].strip().lower() in TOKEN_LOGINS]
    for wanted in sites:
        for row in rows:
            site = host_of(row["site"])
            if site and (site == wanted or host_matches(site, wanted)):
                try:
                    return secret_for_browser(session, user, uuid.UUID(row["id"]))
                except VaultError:
                    raise DirectError("vault_unavailable", "Ошибка: хранилище ключей недоступно.") from None
    return None


NO_TOKEN = (
    "Нет OAuth-токена Яндекса в «Доступах». Пользователю: получите токен на oauth.yandex.ru для приложения с "
    "доступом к API Директа (и Метрики), сохраните его в «Доступах»: сайт direct.yandex.ru, логин token, "
    "пароль — токен. Пока его нет, данные можно смотреть в кабинете direct.yandex.ru на экране."
)


def execute(session, user, task_id, name: str, args: dict,
            transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> ClientResult:
    """app.agent.executors contract."""
    token = ""
    try:
        if name == "metrika_read":
            url, params = build_metrika(args)
            token = token_for(session, user, METRIKA_SITES + TOKEN_SITES) or ""
            if not token:
                raise DirectError("no_credential", NO_TOKEN)
            return ClientResult(call_metrika(url, params, token, transport).replace(token, "<CREDENTIAL>"))
        url, headers, body, is_report = build_direct(args)
        token = token_for(session, user, TOKEN_SITES) or ""
        if not token:
            raise DirectError("no_credential", NO_TOKEN)
        text = call_direct(url, headers, body, is_report, token, transport, sleep)
        prefix = "Песочница Директа. " if "sandbox" in url else ""
        return ClientResult(prefix + text.replace(token, "<CREDENTIAL>"))
    except DirectError as exc:
        text = exc.text.replace(token, "<CREDENTIAL>") if token else exc.text
        return ClientResult(text, exc.code)
    except httpx.HTTPError as exc:
        log.info("yandex api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: API Яндекса не ответил.", "api_unavailable")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(DIRECT_TOOL)
    register_client_tool(METRIKA_TOOL)
    register_executor(TOOL_NAMES, execute)
