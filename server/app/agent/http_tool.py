"""http_request: any HTTP call to a public API, with secrets by name and read-only on live accounts.

Secrets. The model writes {{secret:NAME}} in the url, query, headers or body. NAME is a «Доступы» entry:
its site (api-seller.ozon.ru) or site/login (api-seller.ozon.ru/123456). The server swaps in the
entry's password right before the request, and only when the request goes to that entry's host: a
page that talks the model into sending a token to another site gets a refusal, not the token. The
model, the step stream and the trajectory see the placeholder; a secret echoed back in the response
is cut out before the model sees it.

Writes (app.agent.write_policy). GET and HEAD are reads, and so are POSTs to endpoints that read
(READ_ENDPOINTS: Ozon …/list|info, Direct method «get» and reports,
GA4 Data API runReport; Metrika has no POST reads): they
run freely, and so does a POST for an OAuth token (…/oauth/token): it hands out a token, it changes
nothing. Writes to sandboxes and public training APIs (SANDBOX_HOSTS, PUBLIC_DEMO_HOSTS plus
HELI_HTTP_SANDBOX_HOSTS) run at once: reqres.in, petstore, httpbin, api.edu.cdek.ru have no account of
the user's behind them. A write to any other host is a live write: the loop shows a card (method,
host, path, body; for ads and payments a budget warning) and the request goes out only after the
user's «да» for this very call. Hit-collection endpoints of GA4 and Метрика (Measurement
Protocol, mc.yandex.ru/watch) are refused for every method, even GET.

User-Agent. Public APIs (hh.ru, Nominatim) refuse a request without an identifying User-Agent and
blacklist browser ones. The server sends app.web.USER_AGENT unless the caller passes its own, and
replaces an empty or browser-like one (Mozilla/…, Chrome/…). When a service still refuses the
User-Agent, the result says so plainly, so the model fixes the header instead of opening a browser.
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
TOKEN = "token"  # check_policy's mode for an OAuth token request; write_policy sees it as a read
SECRET_RE = re.compile(r"\{\{\s*secret:([^{}]{1,200}?)\s*\}\}")
REDACTED = "<CREDENTIAL>"
# Headers that only the server sets.
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie"}
# Response headers worth showing (Direct sends units and retryIn, Ozon a request id).
_SHOWN_HEADERS = ("content-type", "retryin", "units", "units-used-login", "requestid", "location", "x-request-id")

SANDBOX_HOSTS = ("api-sandbox.direct.yandex.com", "api-sandbox.direct.yandex.ru")
# Public demo and training APIs: there is no account of the user's or Heli's behind them, a POST there
# changes only the service's own test data. Writes run at once, like in a sandbox (dangerous verbs such
# as DELETE still ask). A subdomain matches too (eu.httpbin.org).
PUBLIC_DEMO_HOSTS = (
    "api.edu.cdek.ru",  # СДЭК: официальная учебная среда API v2
    "reqres.in",
    "petstore.swagger.io",
    "petstore3.swagger.io",
    "fakestoreapi.com",
    "httpbin.org",
    "jsonplaceholder.typicode.com",
    "dummyjson.com",
    "postman-echo.com",
    "restful-booker.herokuapp.com",
)
# OAuth token endpoints (/oauth/token, /oauth2/token, /v2/oauth/token, oauth.yandex.ru/token): a POST
# there exchanges credentials for a token and changes nothing in the account.
_TOKEN_PATH = re.compile(r"(?i)/oauth2?/(?:v\d+/)?token/?$")
# Browser User-Agents: services like hh.ru blacklist them for API calls, and the bot is not a browser.
_BROWSER_UA = re.compile(r"(?i)mozilla/|chrome/|safari/|firefox/|edg/|opera/|yabrowser/")
_UA_REJECTED = re.compile(r"(?i)user[-_ ]?agent")
UA_REJECTED_CODE = "user_agent_rejected"


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
    # Google Analytics Data API: reports are POST by protocol and only read. The Admin API has no POST read here:
    # property settings are never changed. Measurement Protocol hits are refused in app.web.check_url.
    ReadRule("analyticsdata.googleapis.com", re.compile(
        r"^/v1(?:beta|alpha)/properties/\d+:(?:runReport|runPivotReport|batchRunReports|batchRunPivotReports"
        r"|runRealtimeReport|checkCompatibility)$"
    )),
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
    return {h.strip().lower() for h in (*SANDBOX_HOSTS, *PUBLIC_DEMO_HOSTS, *extra.split(",")) if h.strip()}


def is_public_demo(host: str) -> bool:
    from app.skills.hosts import host_matches

    return any(host_matches(host, s) for s in PUBLIC_DEMO_HOSTS)


def is_token_request(method: str, host: str, path: str) -> bool:
    """POST for an OAuth token: not a change in the account, on any host."""
    if method != "POST":
        return False
    return bool(_TOKEN_PATH.search(path)) or (host.startswith("oauth.") and path.rstrip("/") == "/token")


def is_sandbox(host: str) -> bool:
    from app.skills.hosts import host_matches

    return any(host_matches(host, s) for s in sandbox_hosts())


def is_read(method: str, host: str, path: str, body) -> bool:
    """True when the call cannot change a live account."""
    if method in READ_METHODS or is_token_request(method, host, path):
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


# Hosts and paths where a write may spend money: ads, bids, payments.
_BUDGET_HOST = re.compile(r"(^|\.)(direct\.yandex\.(com|ru)|api-performance\.ozon\.ru|ads?\.[\w.-]+|advert-api\.wildberries\.ru)$")
_BUDGET_PATH = re.compile(r"(?i)pay|invoice|bid|budget|strateg|campaign|deposit|transfer|refund")


def kind(method: str, url: str, body) -> str:
    """'read', 'sandbox' or 'live' for one request. No network."""
    from urllib.parse import urlsplit

    from app.agent import write_policy as wp

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if is_read(method, host, parts.path or "/", body):
        return wp.READ
    if is_sandbox(host):
        return wp.SANDBOX
    return wp.LIVE


def check_policy(method: str, url: str, body, confirmed: bool = False) -> str:
    """'read', 'sandbox' or 'live'. Raises for a live write the user has not approved."""
    from urllib.parse import urlsplit

    from app.agent import write_policy as wp

    mode = kind(method, url, body)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if mode == wp.READ and is_token_request(method, host, parts.path or "/"):
        return TOKEN  # shown as «получение токена» in the answer
    if mode == wp.READ and is_sandbox(host):
        return wp.SANDBOX  # shown as «песочница» in the answer
    if mode == wp.LIVE and not confirmed:
        raise HttpToolError(
            wp.REFUSED_CODE,
            f"Не выполнено: {method} {host}{parts.path} меняет данные в кабинете клиента на {host}, "
            "а пользователь это изменение не подтвердил. Запрос не отправлен. Без подтверждения можно писать в "
            "песочницы и публичные учебные API (api-sandbox.direct.yandex.com, api.edu.cdek.ru, reqres.in, "
            "petstore.swagger.io, httpbin.org…).",
        )
    return mode


def classify(args: dict):
    """write_policy rule for http_request. Uses the args as the model wrote them: secrets stay placeholders."""
    from urllib.parse import urlsplit

    from app.agent import write_policy as wp

    method = str(args.get("method") or "GET").upper().strip()
    url = str(args.get("url") or "").strip()
    body = args.get("json")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    if body is None and isinstance(args.get("form"), dict):
        body = args["form"]
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    mode = kind(method, url, body)
    verb = f"{method} {path}"
    if isinstance(body, dict) and isinstance(body.get("method"), str):
        verb += f" {body['method']}"
    if mode == wp.READ:
        return wp.read(host)
    if mode == wp.SANDBOX:
        return wp.sandbox(host, verb, verb)
    what = f"{method} {host}{path}"
    if body is not None:
        what += ", тело: " + wp.short(body)
    budget = bool(_BUDGET_HOST.search(host) or _BUDGET_PATH.search(path) or _BUDGET_PATH.search(verb))
    return wp.live(host, what, budget=budget, verb=verb)


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
    "HTTP-запрос к API сервиса (Ozon, Wildberries, Яндекс Директ и Метрика, МойСклад, СДЭК, hh.ru и др.): "
    "метод, адрес, заголовки, JSON-тело (json) или форма (form, application/x-www-form-urlencoded). Ключи не "
    "печатай и не проси: пиши {{secret:ИМЯ}}, где ИМЯ — сайт записи в «Доступах» (например "
    "{{secret:api-seller.ozon.ru}}) или сайт/логин; сервер подставит ключ только для запросов на этот сайт. "
    "Чтение (GET и читающие POST: Ozon …/list, …/info; Директ method get и reports) и POST за OAuth-токеном "
    "(…/oauth/token) — свободно. Запись в песочницы и публичные учебные API (api-sandbox.direct.yandex.com, "
    "api.edu.cdek.ru, reqres.in, petstore.swagger.io, fakestoreapi.com, httpbin.org, "
    "jsonplaceholder.typicode.com) — сразу: это не кабинет клиента, вызывай и показывай ответ API как есть, "
    "браузер для этого не нужен. Запись в кабинет клиента (CRM, Директ, Метрика, МойСклад, Ozon…) — сервер "
    "сам покажет пользователю карточку подтверждения, сам ask_confirmation для этого не вызывай. "
    "User-Agent сервер ставит свой (HeliBot); браузерный User-Agent не подставляй. "
    "Перед первым запросом к незнакомому API найди его документацию (web_search, open_url).",
    {
        "method": {"type": "string", "enum": sorted(METHODS)},
        "url": {"type": "string", "description": "Полный адрес https://…"},
        "query": {"type": "object", "description": "Параметры строки запроса"},
        "headers": {"type": "object", "description": "Заголовки, например {\"Api-Key\": \"{{secret:api-seller.ozon.ru}}\"}"},
        "json": {"description": "JSON-тело запроса"},
        "form": {"type": "object", "description": "Тело формой (x-www-form-urlencoded), например "
                 "{\"grant_type\": \"client_credentials\", \"client_id\": \"…\"}. Вместе с json не передавай"},
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
    form: dict | None = None


def prepare(args: dict, lookup, resolver=None) -> tuple[Prepared, list[str]]:
    """Validate, apply the write policy and fill in secrets. Never makes a request."""
    from app.agent import write_policy as wp

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
    form = args.get("form")
    if not isinstance(headers, dict) or not isinstance(query, dict) or not isinstance(form, (dict, type(None))):
        raise HttpToolError("bad_args", "Ошибка: headers, query и form — объекты.")
    if form is not None and body is not None:
        raise HttpToolError("bad_args", "Ошибка: передай тело либо в json, либо в form, не оба сразу.")
    if form is not None:
        form = {str(k): "" if v is None else str(v) for k, v in form.items()}
    headers = with_user_agent({str(k): str(v) for k, v in headers.items() if str(k).lower() not in _FORBIDDEN_HEADERS})
    try:
        url = check_url(raw_url, resolver) if resolver else check_url(raw_url)
    except WebError as exc:
        if exc.code == "tracking_hit":
            raise HttpToolError(
                "tracking_forbidden",
                "Ошибка: это адрес сбора статистики (хиты GA4 / Метрики). Запрос туда записывает визит или событие в "
                "чужую статистику, поэтому не отправляется ни на боевой сайт, ни «для проверки». Читай отчёты "
                "(ga4_read, metrika_read); тестовое событие — только ga4_send_event на учебный Measurement ID.",
            ) from None
        raise HttpToolError(exc.code, f"Ошибка: адрес недоступен ({exc.code}).") from None
    host = (urlsplit(url).hostname or "").lower()
    mode = check_policy(method, url, body if body is not None else form, confirmed=wp.confirmed(args))
    url_filled, a = resolve_secrets(url, host, lookup)
    query, b = resolve_secrets(query, host, lookup)
    headers, c = resolve_secrets(headers, host, lookup)
    body, d = resolve_secrets(body, host, lookup)
    form, e = resolve_secrets(form, host, lookup)
    used = a + b + c + d + e
    payload = body if body is not None else form
    if payload is not None and len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        raise HttpToolError("body_too_large", "Ошибка: тело запроса больше 200 КБ.")
    return Prepared(method, str(url_filled), host, query, headers, body, mode, form), used


def with_user_agent(headers: dict) -> dict:
    """headers with an identifying User-Agent: the caller's own, or app.web.USER_AGENT when it is
    missing, empty or a browser's (hh.ru blacklists those; the bot does not pretend to be a browser)."""
    from app.web import USER_AGENT

    own = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
    out = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    if own is not None and own.strip() and not _BROWSER_UA.search(own):
        out["User-Agent"] = own.strip()
    else:
        out["User-Agent"] = USER_AGENT
    return out


def send(req: Prepared, transport: httpx.BaseTransport | None = None) -> httpx.Response:
    with httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        return client.request(
            req.method,
            req.url,
            params=req.query or None,
            headers=with_user_agent(req.headers),
            json=req.body if req.body is not None else None,
            data=req.form if req.form is not None else None,
        )


def user_agent_rejected(response: httpx.Response) -> bool:
    """400/403 whose body talks about the User-Agent (hh.ru: bad_user_agent, Nominatim: User-Agent policy)."""
    if response.status_code not in (400, 403) or response.request.method == "HEAD":
        return False
    return bool(_UA_REJECTED.search(response.text[:4000]))


def user_agent_hint(response: httpx.Response) -> str:
    sent = response.request.headers.get("user-agent", "")
    return (
        f"Нужен свой User-Agent: сервис отверг заголовок User-Agent (сервер отправил «{sent}»). Это не данные и не "
        "повод открывать браузер. Посмотри в документации API, какой User-Agent он требует (обычно название "
        "приложения и контакт, у hh.ru ещё есть заголовок HH-User-Agent), и повтори http_request с ним в headers. "
        "Браузерный User-Agent (Chrome, Firefox) не подставляй: такие сервисы держат его в чёрном списке."
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
    label = {
        "sandbox": "песочница или учебный API",
        "live": "кабинет клиента, изменение подтверждено",
        TOKEN: "получение токена, данные не меняются",
    }.get(mode, "чтение")
    head = f"HTTP {response.status_code} ({label})"
    if shown:
        head += "\n" + "\n".join(shown)
    if user_agent_rejected(response):
        head = f"{user_agent_hint(response)}\n\n{head}"
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
        if user_agent_rejected(response):
            error = UA_REJECTED_CODE
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


def _register_policy() -> None:
    from app.agent import write_policy

    write_policy.register("http_request", classify)


_register_policy()
