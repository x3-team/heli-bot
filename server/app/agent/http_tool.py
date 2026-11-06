"""http_request: any HTTP call to a public API, with secrets by name and read-only on live accounts.

Secrets. The model writes {{secret:NAME}} in the url, query, headers or body. NAME is a «Доступы» entry:
its site (api-seller.ozon.ru) or site/login (api-seller.ozon.ru/123456). The server swaps in the
entry's password right before the request, and only when the request goes to that entry's host: a
page that talks the model into sending a token to another site gets a refusal, not the token. The
model, the step stream and the trajectory see the placeholder; a secret echoed back in the response
is cut out before the model sees it.

Read-only. Live accounts are read only. GET and HEAD are reads. POST/PUT/PATCH/DELETE are allowed on
a live host only for endpoints that read (READ_ENDPOINTS: Ozon …/list|info, Direct method «get» and
reports, Metrika has no POST reads). Writes go only to sandboxes (SANDBOX_HOSTS plus
HELI_HTTP_SANDBOX_HOSTS). There is no switch that turns writes on for a live account.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass

import httpx

from app.agent.control import ClientResult

log = logging.getLogger(__name__)

TIMEOUT_S = 30
RESULT_CHARS = 8000
MAX_BODY_BYTES = 200_000
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
READ_METHODS = {"GET", "HEAD"}
SECRET_RE = re.compile(r"\{\{\s*secret:([^{}]{1,200}?)\s*\}\}")
REDACTED = "<CREDENTIAL>"
# Headers that only the server sets.
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie"}
# Response headers worth showing (Direct sends units and retryIn, Ozon a request id).
_SHOWN_HEADERS = ("content-type", "retryin", "units", "units-used-login", "requestid", "location", "x-request-id")

SANDBOX_HOSTS = ("api-sandbox.direct.yandex.com", "api-sandbox.direct.yandex.ru")


@dataclass(frozen=True)
class ReadRule:
    """A POST (or other non-GET) that reads on a live host."""

    host: str
    path: re.Pattern
    # For JSON-RPC style APIs (Direct v5): the body's "method" must be one of these.
    body_methods: tuple[str, ...] = ()


_OZON_READ = re.compile(
    r"^/v\d+/(?:[\w-]+/)*(?:list|info|get|tree|totals|attributes|values)(?:/[\w-]+)*/?$"
    r"|^/v\d+/analytics/(?:data|stock_on_warehouses|turnover/stocks)/?$"
    r"|^/v\d+/finance/(?:transaction/(?:list|totals)|realization|cash-flow-statement/list)/?$"
)
_OZON_WRITE_WORDS = re.compile(r"/(?:create|update|delete|import|set|archive|unarchive|cancel|ship|activate|deactivate|upload)(?:/|$)")

READ_ENDPOINTS: tuple[ReadRule, ...] = (
    ReadRule("api-seller.ozon.ru", _OZON_READ),
    ReadRule("api-performance.ozon.ru", re.compile(r"^/api/client/(?:campaign|statistics/(?:list|daily|json))(?:/[\w-]+)*/?$")),
    # Direct API v5: every service reads with method «get»; Reports is a read by itself.
    ReadRule("api.direct.yandex.com", re.compile(r"^/json/v5/(?!reports)[a-z]+/?$"), ("get", "checkCampaigns", "checkDictionaries", "check")),
    ReadRule("api.direct.yandex.ru", re.compile(r"^/json/v5/(?!reports)[a-z]+/?$"), ("get", "checkCampaigns", "checkDictionaries", "check")),
    ReadRule("api.direct.yandex.com", re.compile(r"^/json/v5/reports/?$")),
    ReadRule("api.direct.yandex.ru", re.compile(r"^/json/v5/reports/?$")),
    # Wildberries statistics and content: POST list/filter endpoints that only read.
    ReadRule("content-api.wildberries.ru", re.compile(r"^/content/v\d+/(?:get/cards/list|cards/limits|directory/[\w-]+)/?$")),
    ReadRule("seller-analytics-api.wildberries.ru", re.compile(r"^/api/v\d+/nm-report/(?:detail|grouped)(?:/history)?/?$")),
)


class HttpToolError(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.text = text or f"Ошибка: {code}"


def sandbox_hosts() -> set[str]:
    extra = os.environ.get("HELI_HTTP_SANDBOX_HOSTS", "")
    return {h.strip().lower() for h in (*SANDBOX_HOSTS, *extra.split(",")) if h.strip()}


def is_sandbox(host: str) -> bool:
    from app.skills.hosts import host_matches

    return any(host_matches(host, s) for s in sandbox_hosts())


def is_read(method: str, host: str, path: str, body) -> bool:
    """True when the call cannot change a live account."""
    if method in READ_METHODS:
        return True
    if method != "POST":
        return False  # PUT, PATCH, DELETE always write
    for rule in READ_ENDPOINTS:
        if host != rule.host or not rule.path.match(path):
            continue
        if rule.host.endswith("ozon.ru") and _OZON_WRITE_WORDS.search(path):
            return False
        if rule.body_methods:
            name = body.get("method") if isinstance(body, dict) else None
            if name not in rule.body_methods:
                continue
        return True
    return False


def check_policy(method: str, url: str, body) -> str:
    """'read' or 'sandbox'. Raises for a write to a live account."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if is_sandbox(host):
        return "sandbox"
    if is_read(method, host, parts.path or "/", body):
        return "read"
    raise HttpToolError(
        "write_forbidden",
        f"Ошибка: {method} {host}{parts.path} меняет данные боевого аккаунта, а боевые аккаунты только для "
        "чтения. Запись можно проверить в песочнице (для Директа — api-sandbox.direct.yandex.com). "
        "Если пользователю нужно изменение в бою, опиши, что и где поменять, и предложи сделать это ему.",
    )


# --- secrets -----------------------------------------------------------------------


def _find_secret(rows: list[dict], name: str) -> dict | None:
    from app.skills.hosts import host_of

    name = name.strip()
    site, _, login = name.partition("/") if "://" not in name else (name, "", "")
    site_host = host_of(site)
    for row in rows:
        if host_of(row["site"]) != site_host and row["site"].strip().lower() != site.lower():
            continue
        if login and row["username"].strip().lower() != login.strip().lower():
            continue
        return row
    return None


def resolve_secrets(value, host: str, lookup) -> tuple[object, list[str]]:
    """value with {{secret:NAME}} replaced. lookup(name) -> (site, secret) or None. Returns (value, secrets used)."""
    from app.skills.hosts import host_matches, host_of

    used: list[str] = []

    def swap(text: str) -> str:
        def one(match: re.Match) -> str:
            name = match.group(1)
            found = lookup(name)
            if found is None:
                raise HttpToolError(
                    "secret_not_found",
                    f"Ошибка: в «Доступах» нет записи «{name}». Попроси пользователя сохранить ключ "
                    "(сайт — адрес API, логин — Client-Id или token, пароль — ключ).",
                )
            site, secret = found
            site_host = host_of(site)
            if not site_host or not (host_matches(host, site_host) or host_matches(site_host, host)):
                raise HttpToolError(
                    "secret_wrong_site",
                    f"Ошибка: ключ «{name}» можно отправлять только на {site_host or site}, а запрос идёт на {host}.",
                )
            used.append(secret)
            return secret

        return SECRET_RE.sub(one, text)

    def walk(item):
        if isinstance(item, str):
            return swap(item)
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item

    return walk(value), used


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


# --- tool --------------------------------------------------------------------------


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL = _fn(
    "http_request",
    "HTTP-запрос к API сервиса (Ozon, Wildberries, Яндекс Директ и Метрика, МойСклад и др.): метод, адрес, "
    "заголовки, JSON-тело. Ключи не печатай и не проси: пиши {{secret:ИМЯ}}, где ИМЯ — сайт записи в "
    "«Доступах» (например {{secret:api-seller.ozon.ru}}) или сайт/логин; сервер подставит ключ только для "
    "запросов на этот сайт. Боевые аккаунты только для чтения: GET и читающие POST (Ozon …/list, …/info; "
    "Директ method get и reports). Изменения — только в песочнице (api-sandbox.direct.yandex.com). "
    "Перед первым запросом к незнакомому API найди его документацию (web_search, open_url).",
    {
        "method": {"type": "string", "enum": sorted(METHODS)},
        "url": {"type": "string", "description": "Полный адрес https://…"},
        "query": {"type": "object", "description": "Параметры строки запроса"},
        "headers": {"type": "object", "description": "Заголовки, например {\"Api-Key\": \"{{secret:api-seller.ozon.ru}}\"}"},
        "json": {"description": "JSON-тело запроса"},
    },
    ["method", "url"],
)
TOOL_NAMES = ("http_request",)


@dataclass
class Prepared:
    method: str
    url: str
    host: str
    query: dict
    headers: dict
    body: object
    mode: str


def prepare(args: dict, lookup, resolver=None) -> tuple[Prepared, list[str]]:
    """Validate, apply the read-only policy and fill in secrets. Never makes a request."""
    from urllib.parse import urlsplit

    from app.web import WebError, check_url

    method = str(args.get("method") or "GET").upper().strip()
    if method not in METHODS:
        raise HttpToolError("bad_method", "Ошибка: метод должен быть GET, HEAD, POST, PUT, PATCH или DELETE.")
    raw_url = str(args.get("url") or "").strip()
    if not raw_url.lower().startswith(("https://", "http://")):
        raise HttpToolError("bad_url", "Ошибка: нужен полный адрес https://…")
    if SECRET_RE.search(urlsplit(raw_url).netloc):
        raise HttpToolError("secret_wrong_field", "Ошибка: ключ нельзя подставлять в адрес хоста.")
    headers = args.get("headers") or {}
    query = args.get("query") or {}
    body = args.get("json")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    if not isinstance(headers, dict) or not isinstance(query, dict):
        raise HttpToolError("bad_args", "Ошибка: headers и query — объекты.")
    headers = {str(k): str(v) for k, v in headers.items() if str(k).lower() not in _FORBIDDEN_HEADERS}
    try:
        url = check_url(raw_url, resolver) if resolver else check_url(raw_url)
    except WebError as exc:
        raise HttpToolError(exc.code, f"Ошибка: адрес недоступен ({exc.code}).") from None
    host = (urlsplit(url).hostname or "").lower()
    mode = check_policy(method, url, body)
    used: list[str] = []
    url_filled, a = resolve_secrets(url, host, lookup)
    query, b = resolve_secrets(query, host, lookup)
    headers, c = resolve_secrets(headers, host, lookup)
    body, d = resolve_secrets(body, host, lookup)
    used = a + b + c + d
    if body is not None and len(json.dumps(body, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        raise HttpToolError("body_too_large", "Ошибка: тело запроса больше 200 КБ.")
    return Prepared(method, str(url_filled), host, query, headers, body, mode), used


def send(req: Prepared, transport: httpx.BaseTransport | None = None) -> httpx.Response:
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        return client.request(
            req.method,
            req.url,
            params=req.query or None,
            headers={"User-Agent": "HeliBot/0.1", **req.headers},
            json=req.body if req.body is not None else None,
        )


# A refused key is not an answer: the model must not turn it into numbers.
AUTH_NOTE = (
    "\n\n[Heli] Авторизация не прошла: это не данные сервиса. Не выдумывай ответ и цифры — скажи "
    "пользователю, что ключ не подошёл, просрочен или не даёт доступа, и попроси обновить его в «Доступах»."
)


def format_response(response: httpx.Response, mode: str, secrets: list[str]) -> str:
    shown = [f"{k}: {response.headers[k]}" for k in _SHOWN_HEADERS if k in response.headers]
    text = response.text if response.request.method != "HEAD" else ""
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            text = json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            pass
    head = f"HTTP {response.status_code} ({'песочница' if mode == 'sandbox' else 'чтение'})"
    if shown:
        head += "\n" + "\n".join(shown)
    if response.status_code in (401, 403):
        text = AUTH_NOTE.strip() + "\n\n" + text  # first, so a long body cut at RESULT_CHARS keeps it
    out = redact(f"{head}\n\n{text}", secrets)
    return out if len(out) <= RESULT_CHARS else out[:RESULT_CHARS] + "\n…[ответ обрезан]"


def vault_lookup(session, user):
    from app.vault import VaultError, list_credentials, secret_for_browser

    rows = list_credentials(session, user)

    def lookup(name: str):
        row = _find_secret(rows, name)
        if row is None:
            return None
        try:
            return row["site"], secret_for_browser(session, user, uuid.UUID(row["id"]))
        except VaultError as exc:
            raise HttpToolError(exc.code, "Ошибка: хранилище ключей недоступно.") from None

    return lookup


def execute(
    session, user, task_id, name: str, args: dict,
    transport: httpx.BaseTransport | None = None, resolver=None,
) -> ClientResult:
    """app.agent.executors contract. transport and resolver are for tests."""
    secrets: list[str] = []
    try:
        req, secrets = prepare(args, vault_lookup(session, user), resolver=resolver)
        response = send(req, transport)
        error = None if response.status_code < 400 else f"http_{response.status_code}"
        return ClientResult(format_response(response, req.mode, secrets), error)
    except HttpToolError as exc:
        return ClientResult(redact(exc.text, secrets), exc.code)
    except httpx.HTTPError as exc:
        log.info("http_request failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: сервис не ответил.", "api_unavailable")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(TOOL)
    register_executor(TOOL_NAMES, execute)
