"""http_request and public APIs (run 06.11.2026, поток E).

Write guard: public training and demo APIs (reqres.in, api.edu.cdek.ru, petstore, httpbin…) are not a
client's account, so POST/PUT/PATCH there run at once; a POST for an OAuth token is not a write at all;
a live host (api.direct.yandex.com, online.moysklad.ru, a client's CRM) still asks.

User-Agent: the server sends an identifying HeliBot User-Agent instead of none or a browser's, and a
refusal over the User-Agent says «нужен свой User-Agent» instead of pushing the model to a browser.

Calls go through the real loop and the real http_request executor; the network is a MockTransport.
No real accounts or keys: everything below is made up.
"""

import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.agent import http_tool, write_policy
from app.agent.guard import Guard
from app.agent.loop import Budget, ToolResult, run_agent
from app.agent.task_run import CONFIRM_HINT
from app.llm.client import Completion, ToolCall
from app.models import User
from app.vault import store_credential
from app.web import USER_AGENT

KEY = base64.b64encode(b"k" * 32).decode()
CRM_SECRET = "crm-secret-123456"
PUBLIC_DNS = lambda host, port: [(0, 0, 0, "", ("93.158.134.3", port))]  # noqa: E731
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TOOLS = [
    {"type": "function", "function": {"name": n, "parameters": {}}}
    for n in ("ask_confirmation", "http_request", "browser_open", "browser_read", "web_search")
]


def _completion(calls=None, text=""):
    return Completion(
        alias="heli-fast", text=text, input_tokens=1, output_tokens=1, cost_rub=0,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a)) for i, (n, a) in enumerate(calls or [])],
    )


def _scripted(*turns):
    """Each turn is a list of (tool, args) the model calls; then it answers «Готово»."""
    queue = [_completion(list(t)) for t in turns]
    return lambda messages, tools: queue.pop(0) if queue else _completion(text="Готово")


# --- mock services ----------------------------------------------------------------------


def reqres(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/api/users":
        body = json.loads(request.content)
        return httpx.Response(201, json={**body, "id": "731", "createdAt": "2026-11-06T19:30:00.000Z"})
    return httpx.Response(404, json={})


def hh(request: httpx.Request) -> httpx.Response:
    """Like api.hh.ru on 06.11: no UA → 403, a browser or generic UA → 400 bad_user_agent, HeliBot/* → vacancies."""
    ua = request.headers.get("user-agent", "")
    if not ua:
        return httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    if not ua.startswith("HeliBot/"):
        return httpx.Response(400, json={"errors": [{"type": "bad_user_agent", "value": "blacklisted"}],
                                         "description": "User-Agent is blacklisted"})
    names = ["Python-разработчик", "Backend-разработчик (Python)", "Data engineer"]
    items = [{"id": str(100 + i), "name": n, "employer": {"name": f"Компания {i}"}} for i, n in enumerate(names)]
    return httpx.Response(200, json={"found": 3, "items": items})


def hh_strict(request: httpx.Request) -> httpx.Response:
    """A service that wants its own app header with a contact, so even the default HeliBot UA is refused."""
    if "@" not in request.headers.get("hh-user-agent", ""):
        return httpx.Response(400, json={"errors": [{"type": "bad_user_agent", "value": "missing"}]})
    return httpx.Response(200, json={"found": 1, "items": [{"id": "1", "name": "Аналитик"}]})


class World:
    def __init__(self, session, user, handler, answer="rejected"):
        self.session, self.user, self.handler, self.answer = session, user, handler, answer
        self.requests: list[httpx.Request] = []
        self.calls: list[str] = []
        self.cards: list[dict] = []
        self.results: list[ToolResult] = []

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def sent(self) -> list[str]:
        return [f"{r.method} {r.url.copy_with(query=None)}" for r in self.requests]

    def call_tool(self, cid, name, args):
        self.calls.append(name)
        if name == "ask_confirmation":
            self.cards.append(args)
            return ToolResult(self.answer)
        if name != "http_request":
            raise AssertionError(f"unexpected tool {name}")
        res = http_tool.execute(self.session, self.user, None, name, args,
                                transport=httpx.MockTransport(self._record), resolver=PUBLIC_DNS)
        out = ToolResult(res.content, res.error)
        self.results.append(out)
        return out

    def run(self, *turns, tools=TOOLS):
        return run_agent([], tools=tools, call_tool=self.call_tool, complete=_scripted(*turns), budget=Budget())


@pytest.fixture
def user(session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.delenv("HELI_HTTP_SANDBOX_HOSTS", raising=False)
    row = User(email="public-apis@example.com", consent_pdn=True, consent_training=False, is_admin=False,
               created_at=datetime.now(timezone.utc))
    session.add(row)
    session.flush()
    store_credential(session, row, "api.example-crm.ru", "token", CRM_SECRET)
    session.flush()
    return row


# --- C1: reqres.in POST runs, answer as is ---------------------------------------------------


def test_reqres_post_runs_without_a_card_and_returns_id_and_created_at(session, user) -> None:
    world = World(session, user, reqres)
    world.run([("http_request", {"method": "POST", "url": "https://reqres.in/api/users",
                                  "json": {"name": "Heli", "job": "tester"}})])
    assert world.cards == [] and world.sent == ["POST https://reqres.in/api/users"]
    result = world.results[0]
    assert result.error is None
    assert result.content.startswith("HTTP 201 (песочница или учебный API)")
    payload = json.loads(result.content.split("\n\n", 1)[1])
    assert payload == {"name": "Heli", "job": "tester", "id": "731", "createdAt": "2026-11-06T19:30:00.000Z"}
    assert "боев" not in result.content and write_policy.REFUSED_CODE not in result.content
    assert "browser_open" not in world.calls


# --- C2: edu CDEK: the server holds the token, the model gets a stub ------------------------


EDU_TOKEN = "edu-access-token-REAL-0001"


def cdek_edu_server(request: httpx.Request) -> httpx.Response:
    """api.edu.cdek.ru: a token for the server's own client, orders only with that Bearer token."""
    if request.url.path == "/v2/oauth/token":
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["client_credentials"] and form.get("client_id") == ["server-edu-id"]:
            return httpx.Response(200, json={"access_token": EDU_TOKEN, "token_type": "bearer", "expires_in": 3599})
        return httpx.Response(401, json={"error": "invalid_client"})
    if request.headers.get("authorization") != f"Bearer {EDU_TOKEN}":
        return httpx.Response(401, json={"errors": [{"code": "v2_token_expired"}]})
    if request.method == "POST" and request.url.path == "/v2/orders":
        return httpx.Response(202, json={"entity": {"uuid": "72753031-0000-4000-8000-000000000001"},
                                         "echo": request.headers.get("authorization")})
    return httpx.Response(404, json={})


@pytest.fixture
def cdek_env(monkeypatch):
    from app.agent import cdek_edu

    monkeypatch.setenv("HELI_CDEK_EDU_CLIENT_ID", "server-edu-id")
    monkeypatch.setenv("HELI_CDEK_EDU_CLIENT_SECRET", "server-edu-secret")
    cdek_edu.reset_cache()
    yield
    cdek_edu.reset_cache()


def test_cdek_edu_token_request_gets_a_stub_not_the_token(session, user, cdek_env) -> None:
    world = World(session, user, cdek_edu_server)
    form = {"grant_type": "client_credentials", "client_id": "model-guess", "client_secret": "model-guess"}
    world.run([("http_request", {"method": "POST", "url": "https://api.edu.cdek.ru/v2/oauth/token", "form": form})])
    assert world.cards == []
    result = world.results[0]
    assert result.error is None
    assert result.content.startswith("HTTP 200 (получение токена, данные не меняются)")
    assert EDU_TOKEN not in result.content and "{{secret:api.edu.cdek.ru}}" in result.content
    # Only the server's own token request went out, with the server's client, not the model's guess.
    assert world.sent == ["POST https://api.edu.cdek.ru/v2/oauth/token"]
    assert parse_qs(world.requests[0].content.decode())["client_id"] == ["server-edu-id"]


def test_cdek_edu_placeholder_becomes_the_server_token_and_is_redacted(session, user, cdek_env) -> None:
    world = World(session, user, cdek_edu_server)
    order = {"method": "POST", "url": "https://api.edu.cdek.ru/v2/orders", "json": {"tariff_code": 136},
             "headers": {"Authorization": "Bearer {{secret:api.edu.cdek.ru}}"}}
    world.run([("http_request", order)], [("http_request", order)])
    assert world.cards == []
    assert world.sent == ["POST https://api.edu.cdek.ru/v2/oauth/token", "POST https://api.edu.cdek.ru/v2/orders",
                          "POST https://api.edu.cdek.ru/v2/orders"]  # the token is cached: one token request
    for result in world.results:
        assert result.error is None and result.content.startswith("HTTP 202 (песочница или учебный API)")
        assert EDU_TOKEN not in result.content and "72753031" in result.content


def test_cdek_edu_token_is_never_sent_to_another_host(session, user, cdek_env) -> None:
    world = World(session, user, cdek_edu_server)
    world.run([("http_request", {"method": "GET", "url": "https://httpbin.org/anything",
                                  "headers": {"Authorization": "Bearer {{secret:api.edu.cdek.ru}}"}})])
    assert world.results[0].error == "secret_wrong_site"
    assert all(EDU_TOKEN not in str(r.headers) for r in world.requests)
    assert not any(r.url.host == "httpbin.org" for r in world.requests)


def test_cdek_edu_server_token_refused_is_an_honest_error(session, user, monkeypatch) -> None:
    from app.agent import cdek_edu

    monkeypatch.setenv("HELI_CDEK_EDU_CLIENT_ID", "revoked-id")
    monkeypatch.setenv("HELI_CDEK_EDU_CLIENT_SECRET", "revoked-secret")
    cdek_edu.reset_cache()
    world = World(session, user, cdek_edu_server)
    world.run([("http_request", {"method": "POST", "url": "https://api.edu.cdek.ru/v2/oauth/token",
                                  "form": {"grant_type": "client_credentials"}})])
    assert world.results[0].error == cdek_edu.UNAVAILABLE_CODE and "не выдумывай" in world.results[0].content
    cdek_edu.reset_cache()


def test_cdek_edu_defaults_to_the_public_test_account(monkeypatch) -> None:
    from app.agent import cdek_edu

    monkeypatch.delenv("HELI_CDEK_EDU_CLIENT_ID", raising=False)
    monkeypatch.delenv("HELI_CDEK_EDU_CLIENT_SECRET", raising=False)
    assert cdek_edu.credentials() == (cdek_edu.PUBLIC_CLIENT_ID, cdek_edu.PUBLIC_CLIENT_SECRET)


def test_live_cdek_token_request_is_not_stubbed() -> None:
    from app.agent import cdek_edu

    assert not cdek_edu.is_token_call({"method": "POST", "url": "https://api.cdek.ru/v2/oauth/token"})
    assert cdek_edu.is_token_call({"method": "POST", "url": "https://api.edu.cdek.ru/v2/oauth/token/"})


@pytest.mark.parametrize(
    "url",
    [
        "https://api.edu.cdek.ru/v2/oauth/token",
        "https://api.cdek.ru/v2/oauth/token",  # live CDEK: a token is still not a change in the account
        "https://oauth.yandex.ru/token",
        "https://auth.example.com/oauth2/token",
    ],
)
def test_oauth_token_requests_are_reads(url) -> None:
    args = {"method": "POST", "url": url, "form": {"grant_type": "client_credentials"}}
    assert write_policy.classify("http_request", args).kind == write_policy.READ
    assert not Guard().needs_approval("http_request", args)
    assert http_tool.check_policy("POST", url, {"grant_type": "client_credentials"}) == http_tool.TOKEN


def test_token_like_paths_that_change_things_are_not_reads() -> None:
    for url in ("https://api.example-crm.ru/oauth/token/revoke", "https://api.example-crm.ru/v1/tokens"):
        assert Guard().needs_approval("http_request", {"method": "POST", "url": url})
    assert Guard().needs_approval("http_request", {"method": "DELETE", "url": "https://api.cdek.ru/v2/oauth/token"})


# --- C3: live Direct still refuses without approval -----------------------------------------


DIRECT_UPDATE = {"method": "POST", "url": "https://api.direct.yandex.com/json/v5/campaigns",
                 "json": {"method": "update", "params": {"Campaigns": [{"Id": 1}]}}}


def test_live_direct_post_without_approval_is_refused(session, user) -> None:
    world = World(session, user, reqres)
    out = world.run([("http_request", DIRECT_UPDATE)])
    assert world.sent == [] and out.stopped == "confirm"
    assert world.cards[0]["details"].startswith(write_policy.CARD_TITLE + ": api.direct.yandex.com")
    assert write_policy.BUDGET_WARNING in world.cards[0]["details"]


def test_live_direct_post_is_refused_by_the_executor_and_without_a_person(session, user) -> None:
    res = http_tool.execute(session, user, None, "http_request", DIRECT_UPDATE,
                            transport=httpx.MockTransport(reqres), resolver=PUBLIC_DNS)
    assert res.error == write_policy.REFUSED_CODE and "кабинете клиента на api.direct.yandex.com" in res.content
    world = World(session, user, reqres)
    out = world.run([("http_request", DIRECT_UPDATE)], tools=[t for t in TOOLS if t["function"]["name"] != "ask_confirmation"])
    assert world.sent == [] and world.cards == [] and out.stopped == "confirm"


# --- which hosts are which ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", "https://reqres.in/api/users"),
        ("PUT", "https://reqres.in/api/users/2"),
        ("PATCH", "https://reqres.in/api/users/2"),
        ("POST", "https://petstore.swagger.io/v2/pet"),
        ("PUT", "https://petstore3.swagger.io/api/v3/pet"),
        ("POST", "https://fakestoreapi.com/products"),
        ("POST", "https://httpbin.org/post"),
        ("POST", "https://jsonplaceholder.typicode.com/posts"),
        ("POST", "https://api.edu.cdek.ru/v2/orders"),
        ("POST", "https://api-sandbox.direct.yandex.com/json/v5/campaigns"),
    ],
)
def test_public_training_apis_write_without_a_card(method, url) -> None:
    args = {"method": method, "url": url, "json": {"name": "Heli"}}
    assert write_policy.classify("http_request", args).kind == write_policy.SANDBOX
    assert not Guard().needs_approval("http_request", args)
    assert write_policy.card("http_request", args) is None


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", "https://api.direct.yandex.com/json/v5/campaigns"),
        ("POST", "https://api.direct.yandex.ru/json/v5/keywordbids"),
        ("POST", "https://api.moysklad.ru/api/remap/1.2/entity/customerorder"),
        ("POST", "https://online.moysklad.ru/api/remap/1.2/entity/product"),
        ("POST", "https://romashka.bitrix24.ru/rest/1/x/crm.deal.add.json"),
        ("PUT", "https://api-metrika.yandex.net/management/v1/counter/1"),
        ("POST", "https://api.cdek.ru/v2/orders"),  # live CDEK, not the training one
        ("POST", "https://reqres.in.evil.example/api/users"),  # lookalikes are not the demo host
        ("POST", "https://notreqres.in/api/users"),
    ],
)
def test_live_hosts_still_ask(method, url) -> None:
    args = {"method": method, "url": url, "json": {"name": "Heli"}}
    assert write_policy.classify("http_request", args).kind == write_policy.LIVE
    assert Guard().needs_approval("http_request", args)


def test_delete_on_a_demo_host_still_asks_like_in_a_sandbox() -> None:
    assert Guard().needs_approval("http_request", {"method": "DELETE", "url": "https://reqres.in/api/users/2"})


def test_a_demo_host_does_not_get_someone_elses_key(session, user) -> None:
    world = World(session, user, reqres)
    world.run([("http_request", {"method": "POST", "url": "https://reqres.in/api/users",
                                  "headers": {"Authorization": "Bearer {{secret:api.example-crm.ru}}"}, "json": {}})])
    assert world.requests == [] and world.results[0].error == "secret_wrong_site"
    assert CRM_SECRET not in world.results[0].content


def test_form_and_json_together_are_refused(session, user) -> None:
    res = http_tool.execute(session, user, None, "http_request",
                            {"method": "POST", "url": "https://httpbin.org/post", "json": {}, "form": {"a": "1"}},
                            transport=httpx.MockTransport(reqres), resolver=PUBLIC_DNS)
    assert res.error == "bad_args"


# --- B: the prompt does not call training APIs a live account -------------------------------


def test_prompt_names_training_apis_as_not_a_live_account() -> None:
    description = http_tool.TOOL["function"]["description"]
    for host in ("api.edu.cdek.ru", "reqres.in", "petstore.swagger.io", "httpbin.org"):
        assert host in description and host in CONFIRM_HINT
    assert "не кабинет клиента" in description and "не боевой кабинет" in CONFIRM_HINT
    assert "form" in http_tool.TOOL["function"]["parameters"]["properties"]


# --- User-Agent -----------------------------------------------------------------------------


def test_get_without_a_user_agent_goes_out_with_helibot(session, user) -> None:
    world = World(session, user, hh)
    world.run([("http_request", {"method": "GET", "url": "https://api.hh.ru/vacancies", "query": {"text": "python"}})])
    assert len(world.requests) == 1
    sent_ua = world.requests[0].headers["user-agent"]
    assert sent_ua == USER_AGENT and sent_ua.startswith("HeliBot/1.0") and "getheli.ru" in sent_ua


@pytest.mark.parametrize("own", ["", "   ", CHROME_UA, "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"])
def test_empty_or_browser_user_agent_is_replaced(own) -> None:
    headers = http_tool.with_user_agent({"user-agent": own, "Accept": "application/json"})
    assert headers == {"Accept": "application/json", "User-Agent": USER_AGENT}


def test_callers_own_app_user_agent_is_kept() -> None:
    own = "HeliVacancies/1.0 (owner@example.com)"
    assert http_tool.with_user_agent({"User-Agent": own})["User-Agent"] == own


def test_hh_finds_vacancies_with_helibot_ua_even_when_the_model_sends_chrome(session, user) -> None:
    world = World(session, user, hh)
    world.run([("http_request", {"method": "GET", "url": "https://api.hh.ru/vacancies",
                                  "query": {"text": "python", "per_page": 3}, "headers": {"User-Agent": CHROME_UA}})])
    assert world.requests[0].headers["user-agent"] == USER_AGENT
    result = world.results[0]
    assert result.error is None and result.content.startswith("HTTP 200 (чтение)")
    found = json.loads(result.content.split("\n\n", 1)[1])
    assert [v["name"] for v in found["items"]] == ["Python-разработчик", "Backend-разработчик (Python)", "Data engineer"]
    assert not any(c.startswith("browser_") for c in world.calls)


def test_user_agent_refusal_says_so_and_a_retry_with_own_header_works(session, user) -> None:
    world = World(session, user, hh_strict)
    url = "https://api.hh.ru/vacancies"
    world.run(
        [("http_request", {"method": "GET", "url": url})],
        [("http_request", {"method": "GET", "url": url, "headers": {"HH-User-Agent": "HeliBot/1.0 (owner@example.com)"}})],
    )
    first, second = world.results
    assert first.error == http_tool.UA_REJECTED_CODE
    assert first.content.startswith("Нужен свой User-Agent") and USER_AGENT in first.content
    assert "browser_open" not in first.content and "HTTP 400" in first.content
    assert second.error is None and "Аналитик" in second.content
    assert world.calls == ["http_request", "http_request"]  # no browser in between


def test_nominatim_style_refusal_in_html_gets_the_hint(session, user) -> None:
    page = "<html><body>Access blocked: please provide a valid HTTP User-Agent identifying your application</body></html>"
    world = World(session, user, lambda r: httpx.Response(403, text=page, headers={"content-type": "text/html"}))
    world.run([("http_request", {"method": "GET", "url": "https://nominatim.openstreetmap.org/search?q=Москва"})])
    assert world.results[0].content.startswith("Нужен свой User-Agent")


def test_other_403_is_not_blamed_on_the_user_agent(session, user) -> None:
    world = World(session, user, lambda r: httpx.Response(403, json={"errors": [{"type": "forbidden"}]}))
    world.run([("http_request", {"method": "GET", "url": "https://api.hh.ru/vacancies"})])
    assert world.results[0].error == "http_403" and "User-Agent" not in world.results[0].content


def test_same_call_after_a_user_agent_refusal_points_to_headers_not_a_browser(session, user) -> None:
    from app.agent.loop import UA_REJECTED

    assert UA_REJECTED == http_tool.UA_REJECTED_CODE
    world = World(session, user, hh_strict)
    seen: list[list[dict]] = []
    turns = [_completion([("http_request", {"method": "GET", "url": "https://api.hh.ru/vacancies"})]) for _ in range(2)]

    def complete(messages, tools):
        seen.append(list(messages))
        return turns.pop(0) if turns else _completion(text="Готово")

    run_agent([], tools=TOOLS, call_tool=world.call_tool, complete=complete, budget=Budget())
    assert len(world.requests) == 1  # the identical repeat is not sent again
    refusal = [m["content"] for m in seen[-1] if m.get("role") == "tool"][-1]
    assert "User-Agent" in refusal and "Браузер для этого не нужен" in refusal
