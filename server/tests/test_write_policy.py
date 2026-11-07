"""One write policy (app.agent.write_policy): read freely, write to sandboxes, write live only after «да».

The calls go through the real loop and the real executors; the network is a MockTransport that records
what would have left the server. No real accounts: hosts and keys below are made up.
"""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.agent import bitrix, http_tool, write_policy, yandex_direct
from app.agent.guard import Guard
from app.agent.loop import Budget, ToolResult, run_agent
from app.llm.client import Completion, ToolCall
from app.models import User
from app.vault import store_credential

KEY = base64.b64encode(b"k" * 32).decode()
DIRECT_TOKEN = "y0_AgAAAA-test-direct-token"
LIVE_HOOK = "https://other.bitrix24.ru/rest/7/livesecret77/"
DEMO_HOOK = "https://b24-lvsc4f.bitrix24.ru/rest/1/demosecret11/"
PUBLIC_DNS = lambda host, port: [(0, 0, 0, "", ("93.158.134.3", port))]  # noqa: E731
TASK = {"method": "tasks.task.add", "params": {"fields": {"TITLE": "Позвонить клиенту", "RESPONSIBLE_ID": 1}}}

TOOLS = [
    {"type": "function", "function": {"name": n, "parameters": {}}}
    for n in ("ask_confirmation", "bitrix24_read", "bitrix24_write", "direct_api", "metrika_read",
              "http_request", "mail_read", "mail_send")
]


def _user(session, email_addr) -> User:
    user = User(email=email_addr, consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def _completion(calls=None, text=""):
    return Completion(
        alias="heli-fast", text=text, input_tokens=1, output_tokens=1, cost_rub=0,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a)) for i, (n, a) in enumerate(calls or [])],
    )


def _scripted(*completions):
    queue = list(completions)
    return lambda messages, tools: queue.pop(0) if queue else _completion(text="Готово")


class World:
    """Runs tools with the real executors. `answer` is the user's reply to every confirmation card."""

    def __init__(self, session, user, answer="rejected"):
        self.session, self.user, self.answer = session, user, answer
        self.sent: list[str] = []
        self.cards: list[dict] = []
        self.results: dict[str, ToolResult] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(f"{request.method} {request.url}")
        if "bitrix24" in request.url.host:
            return httpx.Response(200, json={"result": {"task": {"id": "42"}}})
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}})

    def call_tool(self, cid, name, args):
        if name == "ask_confirmation":
            self.cards.append(args)
            return ToolResult(self.answer)
        transport = httpx.MockTransport(self.handler)
        if name.startswith("bitrix24_"):
            res = bitrix.execute(self.session, self.user, None, name, args, transport=transport)
        elif name in ("direct_api", "metrika_read"):
            res = yandex_direct.execute(self.session, self.user, None, name, args, transport=transport)
        elif name == "http_request":
            res = http_tool.execute(self.session, self.user, None, name, args, transport=transport, resolver=PUBLIC_DNS)
        else:
            raise AssertionError(name)
        out = ToolResult(res.content, res.error)
        self.results[name] = out
        return out


@pytest.fixture
def world(session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.delenv("HELI_BITRIX_SANDBOX_HOSTS", raising=False)
    user = _user(session, "policy@example.com")
    store_credential(session, user, "other.bitrix24.ru", "webhook", LIVE_HOOK)
    store_credential(session, user, "b24-lvsc4f.bitrix24.ru", "webhook", DEMO_HOOK)
    store_credential(session, user, "direct.yandex.ru", "token", DIRECT_TOKEN)
    store_credential(session, user, "api.example-crm.ru", "token", "crm-secret-123456")
    session.flush()
    return World(session, user)


def _run(world, *calls, tools=TOOLS):
    return run_agent([], tools=tools, call_tool=world.call_tool, complete=_scripted(_completion(list(calls))),
                     budget=Budget())


# --- Битрикс24 ------------------------------------------------------------------------


def test_bitrix_write_on_the_training_portal_runs_without_a_question(world) -> None:
    _run(world, ("bitrix24_write", {**TASK, "portal": "b24-lvsc4f.bitrix24.ru"}))
    assert world.cards == []
    assert world.sent == ["POST https://b24-lvsc4f.bitrix24.ru/rest/1/demosecret11/tasks.task.add.json"]
    assert "учебном портале" in world.results["bitrix24_write"].content


def test_bitrix_write_on_a_client_portal_needs_approval(world) -> None:
    out = _run(world, ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}))
    assert world.sent == [] and out.stopped == "confirm"
    card = world.cards[0]
    assert card["action"] == "bitrix24_write"
    assert "other.bitrix24.ru" in card["details"] and "tasks.task.add" in card["details"]
    assert "Позвонить клиенту" in card["details"] and "livesecret77" not in card["details"]


def test_bitrix_live_write_runs_after_approval_and_only_once(world) -> None:
    world.answer = "approved"
    _run(world, ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}))
    assert len(world.cards) == 1
    assert world.sent == ["POST https://other.bitrix24.ru/rest/7/livesecret77/tasks.task.add.json"]
    result = world.results["bitrix24_write"]
    assert result.error is None and "боевом портале" in result.content and "livesecret77" not in result.content


def test_model_approval_does_not_cover_a_live_write(world) -> None:
    """The model asked in its own words; the loop still shows the card with the portal, and «нет» stops it."""
    _run(
        world,
        ("ask_confirmation", {"action": "bitrix24_write", "details": "создать задачу"}),
        ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}),
    )
    assert world.sent == []
    assert len(world.cards) == 1  # the model's card was rejected, so the run stopped before the write


def test_model_approval_then_loop_card(world) -> None:
    answers = iter(["approved", "rejected"])
    original = world.call_tool

    def call_tool(cid, name, args):
        if name == "ask_confirmation":
            world.cards.append(args)
            return ToolResult(next(answers))
        return original(cid, name, args)

    world.call_tool = call_tool
    _run(
        world,
        ("ask_confirmation", {"action": "bitrix24_write", "details": "создать задачу"}),
        ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}),
    )
    assert len(world.cards) == 2 and "other.bitrix24.ru" in world.cards[1]["details"]
    assert world.sent == []


def test_executor_refuses_a_live_write_without_the_loop_flag(world) -> None:
    transport = httpx.MockTransport(world.handler)
    res = bitrix.execute(world.session, world.user, None, "bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"},
                         transport=transport)
    assert res.error == write_policy.REFUSED_CODE and world.sent == []


def test_live_write_without_a_portal_is_refused_even_when_approved(world) -> None:
    """The card could not name the portal, so the approval does not cover it."""
    user = _user(world.session, "onlylive@example.com")
    store_credential(world.session, user, "other.bitrix24.ru", "webhook", LIVE_HOOK)
    world.session.flush()
    res = bitrix.execute(world.session, user, None, "bitrix24_write", {**TASK, write_policy.CONFIRMED_ARG: True},
                         transport=httpx.MockTransport(world.handler))
    assert res.error == write_policy.REFUSED_CODE and world.sent == []
    assert "портал не указан" in Guard().card("bitrix24_write", TASK)["details"]


def test_model_cannot_forge_the_confirmed_flag(world) -> None:
    _run(world, ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru", "_confirmed": True}))
    assert world.sent == [] and len(world.cards) == 1


def test_no_person_to_ask_means_no_live_write(world) -> None:
    tools = [t for t in TOOLS if t["function"]["name"] != "ask_confirmation"]
    out = _run(world, ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}), tools=tools)
    assert world.sent == [] and world.cards == [] and out.stopped == "confirm"


# --- Яндекс Директ ----------------------------------------------------------------------

BID = {"service": "keywordbids", "method": "set", "params": {"KeywordBids": [{"KeywordId": 1, "SearchBid": 15000000}]}}
UPDATE = {"service": "campaigns", "method": "update",
          "params": {"Campaigns": [{"Id": 1, "DailyBudget": {"Amount": 900000000, "Mode": "STANDARD"}}]}}


def test_direct_update_on_the_live_api_needs_approval_and_warns_about_budget(world) -> None:
    out = _run(world, ("direct_api", UPDATE))
    assert world.sent == [] and out.stopped == "confirm"
    details = world.cards[0]["details"]
    assert "api.direct.yandex.com" in details and "campaigns.update" in details and "900000000" in details
    assert write_policy.BUDGET_WARNING in details and DIRECT_TOKEN not in details


def test_direct_update_on_the_live_api_runs_after_approval(world) -> None:
    world.answer = "approved"
    _run(world, ("direct_api", UPDATE))
    assert world.sent == ["POST https://api.direct.yandex.com/json/v5/campaigns"]
    assert "подтверждено" in world.results["direct_api"].content


def test_direct_update_in_the_sandbox_runs_without_a_question(world) -> None:
    _run(world, ("direct_api", {**UPDATE, "sandbox": True}), ("direct_api", {**BID, "sandbox": True}))
    assert world.cards == []
    assert world.sent == ["POST https://api-sandbox.direct.yandex.com/json/v5/campaigns",
                          "POST https://api-sandbox.direct.yandex.com/json/v5/keywordbids"]


def test_direct_delete_in_the_sandbox_still_asks() -> None:
    assert Guard().needs_approval("direct_api", {"service": "ads", "method": "delete", "sandbox": True})
    assert not Guard().needs_approval("direct_api", {"service": "ads", "method": "suspend", "sandbox": True})


def test_direct_executor_refuses_a_live_write_without_the_loop_flag(world) -> None:
    res = yandex_direct.execute(world.session, world.user, None, "direct_api", BID,
                                transport=httpx.MockTransport(world.handler))
    assert res.error == write_policy.REFUSED_CODE and world.sent == []


# --- http_request -----------------------------------------------------------------------

CRM_POST = {"method": "POST", "url": "https://api.example-crm.ru/v1/deals",
            "headers": {"Authorization": "Bearer {{secret:api.example-crm.ru}}"}, "json": {"name": "Сделка", "price": 15000}}


def test_http_write_to_a_live_host_needs_approval(world) -> None:
    _run(world, ("http_request", CRM_POST))
    assert world.sent == []
    details = world.cards[0]["details"]
    assert "api.example-crm.ru" in details and "POST" in details and "15000" in details
    assert "crm-secret-123456" not in details and "Bearer" not in details


def test_http_write_to_a_live_host_runs_after_approval(world) -> None:
    world.answer = "approved"
    _run(world, ("http_request", CRM_POST))
    assert world.sent == ["POST https://api.example-crm.ru/v1/deals"]
    assert "crm-secret-123456" not in world.results["http_request"].content


def test_http_write_to_a_sandbox_runs_and_direct_live_post_warns_about_budget(world) -> None:
    body = {"method": "update", "params": {}}
    _run(world, ("http_request", {"method": "POST", "url": "https://api-sandbox.direct.yandex.com/json/v5/campaigns", "json": body}))
    assert world.cards == [] and len(world.sent) == 1
    card = write_policy.card("http_request", {"method": "POST", "url": "https://api.direct.yandex.com/json/v5/campaigns", "json": body})
    assert card and write_policy.BUDGET_WARNING in card["details"]


def test_http_live_metrika_change_needs_approval() -> None:
    args = {"method": "PUT", "url": "https://api-metrika.yandex.net/management/v1/counter/1/goal/2", "json": {"goal": {}}}
    assert Guard().needs_approval("http_request", args)
    assert Guard().card("http_request", args)["details"].startswith(write_policy.CARD_TITLE + ": api-metrika.yandex.net")


# --- reads never ask --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        ("http_request", {"method": "GET", "url": "https://api-metrika.yandex.net/stat/v1/data?ids=1"}),
        ("http_request", {"method": "HEAD", "url": "https://other.bitrix24.ru/"}),
        ("http_request", {"method": "POST", "url": "https://api.direct.yandex.com/json/v5/campaigns", "json": {"method": "get"}}),
        ("http_request", {"method": "POST", "url": "https://api-seller.ozon.ru/v3/product/list", "json": {}}),
        ("direct_api", {"service": "campaigns", "method": "get"}),
        ("direct_api", {"service": "reports", "params": {}}),
        ("metrika_read", {"report": "stat", "params": {"ids": 1}}),
        ("bitrix24_read", {"method": "crm.deal.list", "portal": "other.bitrix24.ru"}),
        ("mail_read", {"action": "list"}),
    ],
)
def test_reads_never_need_confirmation(name, args) -> None:
    guard = Guard()
    assert not guard.needs_approval(name, args)
    assert guard.card(name, args) is None


def test_bitrix_read_on_a_client_portal_runs_without_a_question(world) -> None:
    _run(world, ("bitrix24_read", {"method": "crm.deal.list", "portal": "other.bitrix24.ru"}))
    assert world.cards == [] and world.sent == ["POST https://other.bitrix24.ru/rest/7/livesecret77/crm.deal.list.json"]


def test_mail_send_still_needs_approval() -> None:
    """Details (HELI_MAIL_SEND=1, one approval = one letter) are in test_mail_assistant.py."""
    assert Guard().needs_approval("mail_send", {"to": "a@example.com", "subject": "x", "body": "y"})


# --- the interface for other integrations -----------------------------------------------


def test_a_new_integration_plugs_in_with_one_classifier(monkeypatch) -> None:
    monkeypatch.setenv("HELI_AMO_SANDBOX_HOSTS", "heli-trial.amocrm.ru")

    def classify(args):
        host = str(args.get("host") or "")
        if args.get("method") == "GET":
            return write_policy.read(host)
        if host in write_policy.hosts_from_env("HELI_AMO_SANDBOX_HOSTS"):
            return write_policy.sandbox(host, "создать сделку", args.get("method", ""))
        return write_policy.live(host, f"создать сделку на {args.get('price')} ₽")

    write_policy.register("demo_write", classify)
    try:
        guard = Guard()
        assert not guard.needs_approval("demo_write", {"host": "client.amocrm.ru", "method": "GET"})
        assert not guard.needs_approval("demo_write", {"host": "heli-trial.amocrm.ru", "method": "POST"})
        assert guard.needs_approval("demo_write", {"host": "heli-trial.amocrm.ru", "method": "DELETE"})
        assert guard.needs_approval("demo_write", {"host": "client.amocrm.ru", "method": "POST", "price": 15000})
        card = guard.card("demo_write", {"host": "client.amocrm.ru", "method": "POST", "price": 15000})
        assert "client.amocrm.ru" in card["details"] and "15000 ₽" in card["details"]
        assert not write_policy.confirmed({"_confirmed": "yes"}) and write_policy.confirmed({"_confirmed": True})
    finally:
        write_policy.unregister("demo_write")


def test_a_broken_classifier_asks_instead_of_guessing() -> None:
    write_policy.register("broken_write", lambda args: 1 / 0)
    try:
        assert Guard().needs_approval("broken_write", {})
    finally:
        write_policy.unregister("broken_write")


# --- Учебный контур: без лишних карточек ------------------------------------------------


def _run_training(world, *calls, tools=TOOLS):
    return run_agent([], tools=tools, call_tool=world.call_tool, complete=_scripted(_completion(list(calls))),
                     budget=Budget(), training=True)


@pytest.mark.parametrize(
    "url,body",
    [
        ("https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party", {"query": "7707083893"}),
        ("https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address", {"query": "Москва Тверская 1"}),
        ("https://egrul.nalog.ru/", {"query": "7707083893"}),
        ("https://statusnpd.nalog.ru/api/v1/tracker/taxpayer_status", {"inn": "500100732259", "requestDate": "2026-11-07"}),
    ],
)
def test_reference_lookups_by_inn_are_reads(url, body) -> None:
    """mix-01, ns-dadata: a company by ИНН through DaData or ФНС is a POST that only reads."""
    args = {"method": "POST", "url": url, "json": body}
    assert not Guard().needs_approval("http_request", args)
    assert write_policy.classify("http_request", args).kind == write_policy.READ


def test_dadata_clean_and_other_posts_are_not_reads() -> None:
    assert Guard().needs_approval("http_request", {"method": "POST", "url": "https://dadata.ru/api/v2/profile/balance/x"})
    assert Guard().needs_approval(
        "http_request", {"method": "DELETE", "url": "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"}
    )


def test_training_account_model_question_is_approved_without_a_person(world) -> None:
    """mix-01: the model asked in its own words before a lookup; on a training account nobody is asked."""
    out = _run_training(world, ("ask_confirmation", {"action": "проверить ИНН в DaData"}),
                        ("bitrix24_write", {**TASK, "portal": "b24-lvsc4f.bitrix24.ru"}))
    assert world.cards == [] and out.stopped is None
    assert world.sent == ["POST https://b24-lvsc4f.bitrix24.ru/rest/1/demosecret11/tasks.task.add.json"]


def test_training_account_risky_tool_runs_without_a_card() -> None:
    tools = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("ask_confirmation", "telegram_send")]
    called = []

    def call_tool(cid, name, args):
        called.append(name)
        return ToolResult("ok")

    out = run_agent([], tools=tools, call_tool=call_tool,
                    complete=_scripted(_completion([("telegram_send", {"text": "x"})])), budget=Budget(), training=True)
    assert called == ["telegram_send"] and out.stopped is None


def test_training_account_still_asks_before_a_live_write(world) -> None:
    """Policy #61: a client's portal is written only after «да», training account or not."""
    out = _run_training(world, ("ask_confirmation", {"action": "bitrix24_write"}),
                        ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}))
    assert world.sent == [] and out.stopped == "confirm"
    assert len(world.cards) == 1 and "other.bitrix24.ru" in world.cards[0]["details"]


def test_training_account_without_a_person_still_no_live_write(world) -> None:
    tools = [t for t in TOOLS if t["function"]["name"] != "ask_confirmation"]
    out = _run_training(world, ("bitrix24_write", {**TASK, "portal": "other.bitrix24.ru"}), tools=tools)
    assert world.sent == [] and out.stopped == "confirm"


def test_bitrix_write_without_portal_goes_to_the_training_webhook(world) -> None:
    """b24-08: the model left out portal; the only webhook is the training portal, so no card."""
    user = _user(world.session, "trainee@example.com")
    store_credential(world.session, user, "b24-lvsc4f.bitrix24.ru", "webhook", DEMO_HOOK)
    world.session.flush()
    world.user = user
    out = run_agent([], tools=TOOLS, call_tool=world.call_tool, complete=_scripted(_completion([("bitrix24_write", TASK)])),
                    budget=Budget(), prepare_args=lambda name, args: bitrix.fill_portal(world.session, user, args))
    assert world.cards == [] and out.stopped is None
    assert world.sent == ["POST https://b24-lvsc4f.bitrix24.ru/rest/1/demosecret11/tasks.task.add.json"]


def test_bitrix_write_without_portal_on_a_client_webhook_names_it_on_the_card(world) -> None:
    user = _user(world.session, "client@example.com")
    store_credential(world.session, user, "other.bitrix24.ru", "webhook", LIVE_HOOK)
    world.session.flush()
    world.user = user
    world.answer = "approved"
    run_agent([], tools=TOOLS, call_tool=world.call_tool, complete=_scripted(_completion([("bitrix24_write", TASK)])),
              budget=Budget(), prepare_args=lambda name, args: bitrix.fill_portal(world.session, user, args))
    assert len(world.cards) == 1 and "other.bitrix24.ru" in world.cards[0]["details"]
    assert world.sent == ["POST https://other.bitrix24.ru/rest/7/livesecret77/tasks.task.add.json"]
