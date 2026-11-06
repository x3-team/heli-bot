"""amocrm_read / amocrm_write: REST API v4 with the token from «Доступов», over a mocked amoCRM.

The same things as for Битрикс24 and Директ: reads go anywhere, writes go at once only on a training
account, on any other account only after the user's «да» on the loop's card, and the token never shows.
"""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.agent import amocrm, bitrix, credentials, write_policy
from app.agent.guard import Guard
from app.agent.loop import Budget, ToolResult, run_agent
from app.llm.client import Completion, ToolCall
from app.models import User
from app.vault import store_credential

KEY = base64.b64encode(b"k" * 32).decode()
TRIAL = "heli-trial.amocrm.ru"
CLIENT = "client-shop.amocrm.ru"
TRIAL_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.trial-token-0123456789"
CLIENT_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.client-token-9876543210"
TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("ask_confirmation", "amocrm_read", "amocrm_write")]

UNSORTED = {
    "_page": 1,
    "_links": {"self": {"href": "https://heli-trial.amocrm.ru/api/v4/leads/unsorted"}},
    "_embedded": {"unsorted": [{
        "uid": "f7a1c2d3", "source_name": "Форма на сайте", "category": "forms", "pipeline_id": 7001,
        "_embedded": {"leads": [{"name": "Заявка: перчатки"}], "contacts": [{"name": "Наталья Крылова"}]},
    }]},
}


def _user(session, email_addr="amo@example.com") -> User:
    user = User(email=email_addr, consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


class Amo:
    """A tiny amoCRM: records every request, answers like API v4."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []
        self.auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.sent.append((request.method, f"{request.url.host}{request.url.path}?{request.url.query.decode()}".rstrip("?"), body))
        self.auth.append(request.headers.get("authorization", ""))
        path = request.url.path
        if path == "/api/v4/leads/unsorted":
            return httpx.Response(200, json=UNSORTED)
        if path == "/api/v4/leads" and request.method == "GET":
            return httpx.Response(204)
        if path == "/api/v4/contacts" and request.method == "POST":
            return httpx.Response(200, json={"_embedded": {"contacts": [{"id": 5001, "request_id": "0"}]}})
        if path == "/api/v4/leads" and request.method == "POST":
            return httpx.Response(200, json={"_embedded": {"leads": [{"id": 5002, "request_id": "0"}]}})
        if path == "/api/v4/leads/5002" and request.method == "PATCH":
            return httpx.Response(200, json={"id": 5002, "updated_at": 1793606100})
        if path == "/api/v4/leads/5002":
            return httpx.Response(200, json={"id": 5002, "status_id": 70002, "pipeline_id": 7001})
        if path == "/api/v4/leads/400":
            return httpx.Response(400, json={"title": "Bad Request", "validation-errors": [{"path": "status_id", "detail": "unknown"}]})
        return httpx.Response(404, json={"title": "Not Found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def amo(session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("HELI_AMO_SANDBOX_HOSTS", TRIAL)
    user = _user(session)
    session.flush()
    return user


def _run(session, user, name, args, api: Amo):
    return amocrm.execute(session, user, None, name, args, transport=api.transport())


# --- read ------------------------------------------------------------------------------


def test_reads_incoming_leads_with_the_token(session, amo) -> None:
    store_credential(session, amo, TRIAL, "token", TRIAL_TOKEN)
    api = Amo()
    result = _run(session, amo, "amocrm_read", {"path": "/api/v4/leads/unsorted", "params": {"limit": 900}}, api)
    assert result.error is None
    assert "Найдено: unsorted 1" in result.content and "Наталья Крылова" in result.content
    assert "_links" not in result.content and TRIAL_TOKEN not in result.content
    assert api.sent == [("GET", f"{TRIAL}/api/v4/leads/unsorted?limit=250", None)]
    assert api.auth == [f"Bearer {TRIAL_TOKEN}"]


def test_read_flattens_filters_and_reports_an_empty_list(session, amo) -> None:
    store_credential(session, amo, f"https://{TRIAL}/leads/pipeline/", "token", TRIAL_TOKEN)
    api = Amo()
    args = {"path": "leads", "params": {"with": ["contacts"], "filter": {"statuses": [{"pipeline_id": 7001, "status_id": 70003}]}}}
    result = _run(session, amo, "amocrm_read", args, api)
    assert result.error is None and "Ничего не найдено" in result.content
    _, url, _ = api.sent[0]
    assert "with=contacts" in url and "filter%5Bstatuses%5D%5B0%5D%5Bstatus_id%5D=70003" in url


@pytest.mark.parametrize(
    "args",
    [
        {"path": "/api/v4/leads", "method": "DELETE"},
        {"path": "/api/v4/leads/unsorted/f7a1c2d3/decline"},
        {"path": "/api/v4/../oauth2/access_token"},
        {"path": "/api/v4/webhooks"},
        {"path": "/api/v4/leads", "params": {"access_token": "x"}},
    ],
)
def test_read_refuses_anything_but_safe_get(session, amo, args) -> None:
    store_credential(session, amo, TRIAL, "token", TRIAL_TOKEN)
    api = Amo()
    result = _run(session, amo, "amocrm_read", args, api)
    assert result.error in {"bad_method", "bad_path", "bad_params"} and api.sent == []


# --- write on the training account ------------------------------------------------------


def test_creates_contact_and_deal_and_moves_the_stage_on_the_trial(session, amo) -> None:
    store_credential(session, amo, TRIAL, "token", TRIAL_TOKEN)
    api = Amo()
    contact = {"method": "POST", "path": "/api/v4/contacts", "account": TRIAL, "body": [
        {"name": "Сергей Орлов", "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": "+7 903 444-55-66"}]}]}]}
    deal = {"method": "POST", "path": "/api/v4/leads", "account": TRIAL, "body": [
        {"name": "Перчатки 50 коробок", "price": 44500, "_embedded": {"contacts": [{"id": 5001}]}}]}
    stage = {"method": "PATCH", "path": "/api/v4/leads/5002", "account": TRIAL, "body": {"status_id": 70002, "pipeline_id": 7001}}
    for args in (contact, deal, stage):
        result = _run(session, amo, "amocrm_write", args, api)
        assert result.error is None and "учебном аккаунте" in result.content and "Проверь запись" in result.content
    check = _run(session, amo, "amocrm_read", {"path": "leads/5002"}, api)
    assert '"status_id": 70002' in check.content
    assert [(m, u) for m, u, _ in api.sent] == [
        ("POST", f"{TRIAL}/api/v4/contacts"),
        ("POST", f"{TRIAL}/api/v4/leads"),
        ("PATCH", f"{TRIAL}/api/v4/leads/5002"),
        ("GET", f"{TRIAL}/api/v4/leads/5002"),
    ]
    assert api.sent[1][2] == deal["body"] and api.sent[2][2] == stage["body"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"method": "DELETE", "path": "/api/v4/leads/5002", "body": {}}, "bad_method"),
        ({"method": "POST", "path": "/api/v4/leads/unsorted/f7a1c2d3/decline", "body": {}}, "bad_path"),
        ({"method": "POST", "path": "/api/v4/leads/5002/unlink", "body": [{"to_entity_id": 1}]}, "bad_path"),
        ({"method": "PATCH", "path": "/api/v4/leads/pipelines/7001", "body": {"name": "x"}}, "bad_path"),
        ({"method": "POST", "path": "/api/v4/leads", "body": [{"name": str(i)} for i in range(11)]}, "too_many"),
        ({"method": "PATCH", "path": "/api/v4/leads", "body": [{"price": 1}]}, "bad_body"),
        ({"method": "GET", "path": "/api/v4/leads", "body": {}}, "bad_method"),
    ],
)
def test_write_refuses_deletes_and_bulk_changes(session, amo, args, code) -> None:
    store_credential(session, amo, TRIAL, "token", TRIAL_TOKEN)
    api = Amo()
    result = _run(session, amo, "amocrm_write", {**args, "account": TRIAL}, api)
    assert result.error == code and api.sent == []


def test_api_errors_are_shown_without_the_token(session, amo) -> None:
    store_credential(session, amo, TRIAL, "token", TRIAL_TOKEN)
    result = _run(session, amo, "amocrm_write", {"method": "PATCH", "path": "leads/400", "account": TRIAL, "body": {"status_id": 1}}, Amo())
    assert result.error == "api_error" and "status_id" in result.content and TRIAL_TOKEN not in result.content


# --- live accounts ---------------------------------------------------------------------


def test_write_to_a_client_account_without_approval_never_leaves(session, amo) -> None:
    store_credential(session, amo, CLIENT, "token", CLIENT_TOKEN)
    api = Amo()
    args = {"method": "POST", "path": "/api/v4/leads", "account": CLIENT, "body": [{"name": "Сделка"}]}
    result = _run(session, amo, "amocrm_write", args, api)
    assert result.error == write_policy.REFUSED_CODE and api.sent == []
    assert CLIENT in result.content and CLIENT_TOKEN not in result.content
    # «да» for another account (or no account in the call) does not cover this one.
    for other in ({**args, "account": "", "_confirmed": True}, {**args, "account": TRIAL, "_confirmed": True}):
        assert _run(session, amo, "amocrm_write", other, api).error in {write_policy.REFUSED_CODE, "no_credential"}
    assert api.sent == []
    done = _run(session, amo, "amocrm_write", {**args, "_confirmed": True}, api)
    assert done.error is None and "боевом аккаунте" in done.content
    assert [(m, u) for m, u, _ in api.sent] == [("POST", f"{CLIENT}/api/v4/leads")]


def test_live_reads_need_no_approval(session, amo) -> None:
    store_credential(session, amo, CLIENT, "token", CLIENT_TOKEN)
    api = Amo()
    assert _run(session, amo, "amocrm_read", {"path": "leads/unsorted", "account": CLIENT}, api).error is None
    assert api.auth == [f"Bearer {CLIENT_TOKEN}"]


def test_policy_classifies_by_the_account_in_the_call(amo) -> None:
    guard = Guard()
    body = [{"name": "Сделка на 15 000", "price": 15000}]
    trial = {"method": "POST", "path": "leads", "body": body, "account": TRIAL}
    client = {**trial, "account": CLIENT}
    assert write_policy.classify("amocrm_write", trial).kind == write_policy.SANDBOX
    assert not guard.needs_approval("amocrm_write", trial)
    assert guard.needs_approval("amocrm_write", client)
    assert guard.needs_approval("amocrm_write", {**trial, "account": ""})  # no account: ask, don't guess
    card = guard.card("amocrm_write", client)
    assert CLIENT in card["details"] and "POST leads" in card["details"] and "15000" in card["details"]
    assert not guard.needs_approval("amocrm_write", {"method": "DELETE", "path": "leads/1", "body": {}, "account": CLIENT})
    assert not guard.needs_approval("amocrm_read", {"path": "leads", "account": CLIENT})


def _completion(calls=None, text=""):
    return Completion(
        alias="heli-fast", text=text, input_tokens=1, output_tokens=1, cost_rub=0,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a)) for i, (n, a) in enumerate(calls or [])],
    )


@pytest.mark.parametrize(("answer", "sent"), [("rejected", 0), ("approved", 1)])
def test_loop_shows_the_card_and_writes_only_after_yes(session, amo, answer, sent) -> None:
    store_credential(session, amo, CLIENT, "token", CLIENT_TOKEN)
    api, cards = Amo(), []

    def call_tool(cid, name, args):
        if name == "ask_confirmation":
            cards.append(args)
            return ToolResult(answer)
        res = _run(session, amo, name, args, api)
        return ToolResult(res.content, res.error)

    queue = [_completion([("amocrm_write", {"method": "PATCH", "path": "leads/5002", "account": CLIENT,
                                             "body": {"status_id": 70002}, "_confirmed": True})])]
    out = run_agent([], tools=TOOLS, call_tool=call_tool, budget=Budget(),
                    complete=lambda messages, tools: queue.pop(0) if queue else _completion(text="Готово"))
    assert len(cards) == 1 and CLIENT in cards[0]["details"] and "status_id" in cards[0]["details"]
    assert len(api.sent) == sent
    assert out.stopped == ("confirm" if answer == "rejected" else None)


# --- access ----------------------------------------------------------------------------


def test_no_token_asks_to_add_it_to_dostupy(session, amo) -> None:
    store_credential(session, amo, "online.moysklad.ru", "token", "moysklad-token-0123456789")
    result = _run(session, amo, "amocrm_read", {"path": "leads"}, Amo())
    assert result.error == "no_credential"
    assert "«Доступах»" in result.content and "долгосрочный токен" in result.content and "в чат не пишите" in result.content


@pytest.mark.parametrize(
    "secret",
    [TRIAL_TOKEN, f"Bearer {TRIAL_TOKEN}", f"https://{TRIAL}/?access_token={TRIAL_TOKEN}"],
)
def test_token_forms(secret) -> None:
    assert amocrm.token_of(secret) == TRIAL_TOKEN


@pytest.mark.parametrize("secret", ["пароль 123", f"https://{TRIAL}/leads/"])
def test_bad_secret_is_explained(session, amo, secret) -> None:
    store_credential(session, amo, TRIAL, "token", secret)
    result = _run(session, amo, "amocrm_read", {"path": "leads"}, Amo())
    assert result.error == "bad_credential" and "«Доступах»" in result.content


@pytest.mark.parametrize(
    ("site", "host"),
    [
        ("heli-trial.amocrm.ru", "heli-trial.amocrm.ru"),
        ("https://shop.amocrm.ru/leads/pipeline/", "shop.amocrm.ru"),
        ("shop.kommo.com", "shop.kommo.com"),
        ("amocrm.ru", ""),
        ("www.amocrm.ru", ""),
        ("shop.bitrix24.ru", ""),
        ("amocrm.ru.evil.example", ""),
    ],
)
def test_account_host(site, host, monkeypatch) -> None:
    monkeypatch.delenv("HELI_AMO_SANDBOX_HOSTS", raising=False)
    assert amocrm.account_host(site) == host


def test_picks_the_named_account_and_bitrix_ignores_amo_tokens(session, amo) -> None:
    store_credential(session, amo, TRIAL, "webhook", TRIAL_TOKEN)
    store_credential(session, amo, CLIENT, "token", CLIENT_TOKEN)
    assert amocrm.token_entry_for(session, amo, CLIENT)["site"] == CLIENT
    assert amocrm.token_entry_for(session, amo, "client-shop")["site"] == CLIENT
    assert amocrm.token_entry_for(session, amo, "nobody.amocrm.ru") is None
    assert bitrix.webhook_for(session, amo, None) is None


def test_find_credential_says_rest_first(session, amo) -> None:
    store_credential(session, amo, TRIAL, "token", TRIAL_TOKEN)
    session.commit()
    text = credentials.execute(session, amo, None, "find_credential", {"site": TRIAL}).content
    assert "amocrm_read" in text and "без входа" in text and TRIAL_TOKEN not in text


def test_playbook_says_rest_first_no_captcha_no_password() -> None:
    from app.skills.playbooks import playbook

    guide = playbook("amocrm").guide
    for words in ("amocrm_read", "amocrm_write", "Капчу не решай", "в чате не проси", "проверь её через amocrm_read"):
        assert words in guide, words


def test_eval_world_trial_is_a_sandbox_only_during_the_scenario(monkeypatch) -> None:
    from evals.runner import load_scenarios, run_scenario

    monkeypatch.delenv("HELI_AMO_SANDBOX_HOSTS", raising=False)
    seen = {}

    def agent(task, tools, call_tool, budget):
        args = {"method": "POST", "path": "leads", "body": [{"name": "x"}], "account": "heli-uchebny.amocrm.ru"}
        seen["needs"] = Guard().needs_approval("amocrm_write", args)
        return {"final_answer": "", "steps": 0, "cost_rub": 0, "stopped": None}

    scenario = next(s for s in load_scenarios() if s["id"] == "amo-01")
    run_scenario(scenario, agent)
    assert seen["needs"] is False
    assert "heli-uchebny.amocrm.ru" not in amocrm.sandbox_hosts()
