"""moysklad_write: товар, приёмка, заказ, счёт через JSON API 1.2 — только в учебном аккаунте.

Все запросы уходят в мок evals/moysklad_mock.py через httpx.MockTransport: живой кабинет не нужен.
"""

import base64
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from app.agent import moysklad
from app.agent.moysklad import Api, MsError, run_read, run_write
from app.models import User
from app.vault import store_credential
from evals.moysklad_mock import MoySkladMock
from evals.world import World

KEY = base64.b64encode(b"k" * 32).decode()
TRAIN = {"id": "ms-train", "site": "online.moysklad.ru", "username": "admin@heli-train", "secret": "train-secret-pass"}
LIVE = {"id": "ms-live", "site": "online.moysklad.ru", "username": "admin@romashka-trade", "secret": "live-secret-pass"}
SANDBOX = {"heli-train"}
PRODUCT = {"action": "create_product", "name": "Перчатки нитриловые M", "article": "GL-M-100", "buy_price": 450, "sale_price": 690}


def _state(*access: dict) -> dict:
    state = json.loads(json.dumps(World.build().state["moysklad"]))
    state["access"] = [dict(a) for a in access]
    return state


def _write(mock: MoySkladMock, args: dict, sandbox: set[str] = SANDBOX):
    creds = [{k: a[k] for k in ("id", "site", "username")} for a in mock.ms["access"]]
    secret = lambda cred: next(a["secret"] for a in mock.ms["access"] if a["id"] == cred["id"])  # noqa: E731
    return run_write(args, creds, secret, mock.transport(), sandbox=sandbox)


def _posts(mock: MoySkladMock) -> list[str]:
    return [path for method, path in mock.requests if method == "POST"]


# --- 1. товар -------------------------------------------------------------------------------------


def test_create_product_posts_returns_id_and_the_second_get_matches() -> None:
    mock = MoySkladMock(_state(TRAIN))
    result = _write(mock, PRODUCT)
    assert result.error is None, result.content
    assert _posts(mock) == ["entity/product"]
    product = next(p for p in mock.ms["products"] if p["article"] == "GL-M-100")
    assert product["name"] == "Перчатки нитриловые M" and product["buy"] == 450 and product["sale"] == 690
    assert f"id {product['id']}" in result.content and "поля совпадают" in result.content
    # После POST запись перечитана по id.
    assert mock.requests.index(("GET", f"entity/product/{product['id']}")) > mock.requests.index(("POST", "entity/product"))
    data = json.loads(result.content.splitlines()[1])
    assert data["buyPrice"] == 450 and data["salePrices"] == [690] and data["article"] == "GL-M-100"
    # moysklad_read по тому же id видит то же самое (цены в рублях, не в копейках).
    again = run_read({"resource": "product", "id": product["id"]}, TRAIN, lambda c: TRAIN["secret"], mock.transport())
    seen = json.loads(again.content.split("\n", 1)[1])
    assert again.error is None and seen["article"] == "GL-M-100" and seen["salePrices"] == [690] and seen["buyPrice"] == 450


def test_product_body_is_in_kopecks_with_a_price_type() -> None:
    sent = []
    mock = MoySkladMock(_state(TRAIN))

    def spy(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(json.loads(request.content))
        return mock.handle(request)

    creds = [{k: TRAIN[k] for k in ("id", "site", "username")}]
    result = run_write(PRODUCT, creds, lambda c: TRAIN["secret"], httpx.MockTransport(spy), sandbox=SANDBOX)
    assert result.error is None
    body = sent[0]
    assert body["buyPrice"]["value"] == 45000 and body["salePrices"][0]["value"] == 69000
    assert body["salePrices"][0]["priceType"]["meta"]["href"].endswith("/pricetype/sale")


def test_same_article_is_not_created_twice() -> None:
    mock = MoySkladMock(_state(TRAIN))
    assert _write(mock, PRODUCT).error is None
    again = _write(mock, PRODUCT)
    assert again.error == "already_exists" and _posts(mock) == ["entity/product"]
    assert len([p for p in mock.ms["products"] if p["article"] == "GL-M-100"]) == 1


# --- 2. приёмка ---------------------------------------------------------------------------------


def test_supply_is_created_and_posted() -> None:
    mock = MoySkladMock(_state(TRAIN))
    result = _write(mock, {"action": "create_supply", "agent": "ООО «СпецПоставка»",
                           "positions": [{"product": "GL-L-100", "quantity": 20, "price": 450}]})
    assert result.error is None, result.content
    supply = mock.ms["supplies"][0]
    assert supply["applicable"] is True and supply["store"] == "00000000-0000-4000-8000-000000000201"
    assert supply["positions"] == [{"product": "00000000-0000-4000-8000-000000000401", "quantity": 20, "price": 450.0}]
    assert next(p for p in mock.ms["products"] if p["article"] == "GL-L-100")["stock"] == 57
    assert f"id {supply['id']}" in result.content and '"applicable": true' in result.content


def test_supply_price_defaults_to_the_buy_price_and_can_stay_unposted() -> None:
    mock = MoySkladMock(_state(TRAIN))
    result = _write(mock, {"action": "create_supply", "agent": "7701112223", "applicable": False,
                           "positions": [{"product": "Лента упаковочная 48 мм", "quantity": 3}]})
    assert result.error is None, result.content
    supply = mock.ms["supplies"][0]
    assert supply["applicable"] is False and supply["positions"][0]["price"] == 60
    assert next(p for p in mock.ms["products"] if p["article"] == "TAPE-48")["stock"] == 8


# --- 3. заказ покупателя и счёт -----------------------------------------------------------------


def test_customer_order_and_invoice_both_return_ids() -> None:
    mock = MoySkladMock(_state(TRAIN))
    order = _write(mock, {"action": "create_customerorder", "agent": "502712345678",
                          "positions": [{"product": "TAPE-48", "quantity": 5}]})
    assert order.error is None, order.content
    order_id = mock.ms["customerorders"][0]["id"]
    assert mock.ms["customerorders"][0]["positions"][0]["price"] == 95  # цена продажи из карточки
    invoice = _write(mock, {"action": "create_invoiceout", "customerorder_id": order_id})
    assert invoice.error is None, invoice.content
    doc = mock.ms["invoices"][0]
    assert doc["customerorder"] == order_id and doc["agent"] == mock.ms["customerorders"][0]["agent"]
    assert doc["positions"] == mock.ms["customerorders"][0]["positions"]
    assert f"id {order_id}" in order.content and f"id {doc['id']}" in invoice.content and order_id != doc["id"]
    assert _posts(mock) == ["entity/customerorder", "entity/invoiceout"]


def test_ambiguous_or_missing_counterparty_is_asked_not_guessed() -> None:
    mock = MoySkladMock(_state(TRAIN))
    missing = _write(mock, {"action": "create_customerorder", "agent": "ООО «Нет такого»",
                            "positions": [{"product": "TAPE-48", "quantity": 1}]})
    assert missing.error == "not_found" and _posts(mock) == []
    mock.ms["counterparties"].append({"id": "00000000-0000-4000-8000-000000000303", "name": "ИП Соколов Пётр", "inn": "502700000001"})
    twins = _write(mock, {"action": "create_customerorder", "agent": "Сокол",
                          "positions": [{"product": "TAPE-48", "quantity": 1}]})
    assert twins.error == "ambiguous" and _posts(mock) == []


# --- 4. без экрана, когда API отвечает 200 ------------------------------------------------------


def test_prod_loop_writes_through_the_api_without_the_browser() -> None:
    """Прод-цикл и прод-промпт на мире с браузером: запись идёт API, браузер не трогается, цикл не
    подталкивает к форме (нет «нажми Сохранить» и подобных напоминаний)."""
    from app.agent import evals as prod_evals
    from app.llm.client import Completion, ToolCall

    world = World.build({"browser": True})
    seen: list[list[dict]] = []

    class Script:
        def complete(self, alias, messages, max_tokens=None, tools=None):
            seen.append(messages)
            if len(seen) == 1:
                return Completion(alias, "", 10, 10, 0, [ToolCall("c1", "moysklad_write", json.dumps(PRODUCT, ensure_ascii=False))])
            return Completion(alias, "Товар GL-M-100 заведён.", 10, 10, 0)

    out = prod_evals.run("Заведи в МоёмСкладе товар «Перчатки нитриловые M», артикул GL-M-100", world.tools(),
                         world.call, {"max_steps": 6}, client=Script())
    assert out["stopped"] is None and [c.tool for c in world.calls] == ["moysklad_write"]
    assert not any(c.tool.startswith("browser_") for c in world.calls)
    system = seen[0][0]["content"]
    assert "moysklad_write" in system and "не пиши об успехе" in system
    assert "поля совпадают" in seen[1][-1]["content"]
    assert len(seen) == 2  # без лишних раундов-напоминаний


def test_moysklad_guide_and_tools_send_writes_to_the_api() -> None:
    from app.skills.playbooks import _MOYSKLAD_GUIDE

    assert "moysklad_write" in _MOYSKLAD_GUIDE and "Создавать и менять — только через экран" not in _MOYSKLAD_GUIDE
    assert "moysklad_write" in moysklad.TOOL["function"]["description"]
    assert "не изображай успех" in moysklad.WRITE_TOOL["function"]["description"]


# --- 5. не учебный аккаунт / не из «Доступов» ---------------------------------------------------


def test_live_account_is_refused_before_any_request() -> None:
    mock = MoySkladMock(_state(LIVE))
    result = _write(mock, PRODUCT)
    assert result.error == "write_forbidden" and "romashka-trade" in result.content and "Ничего не создано" in result.content
    assert mock.requests == [] and len(mock.ms["products"]) == 2


def test_account_not_in_access_is_refused() -> None:
    mock = MoySkladMock(_state(TRAIN, LIVE))
    for account in ("romashka", "admin@other-shop", "evil.example.com"):
        result = _write(mock, {**PRODUCT, "account": account})
        assert result.error == "not_in_access", (account, result.content)
    assert _write(mock, {**PRODUCT, "account": "romashka-trade"}).error == "write_forbidden"
    assert mock.requests == []
    # Хост МоегоСклада вместо аккаунта — значит «аккаунт не указан»: берётся учебный.
    assert _write(mock, {**PRODUCT, "account": "online.moysklad.ru"}).error is None


def test_credential_for_another_host_is_not_a_moysklad_access() -> None:
    mock = MoySkladMock(_state({**TRAIN, "site": "moysklad.evil.example.com"}))
    result = _write(mock, PRODUCT)
    assert result.error == "no_credential" and mock.requests == []


def test_the_account_behind_the_login_is_checked_by_the_api() -> None:
    # Логин говорит «учебный», а API отвечает, что это другой (боевой) аккаунт: записи нет.
    mock = MoySkladMock(_state({**TRAIN, "uid": "admin@romashka-trade"}))
    result = _write(mock, PRODUCT)
    assert result.error == "write_forbidden" and _posts(mock) == []
    # Вход по токену: аккаунт известен только из API.
    token = {"id": "ms-token", "site": "online.moysklad.ru", "username": "token", "secret": "tok-123", "uid": "admin@heli-train"}
    assert _write(MoySkladMock(_state(token)), PRODUCT).error is None
    live_token = MoySkladMock(_state({**token, "uid": "admin@romashka-trade"}))
    assert _write(live_token, PRODUCT).error == "write_forbidden" and _posts(live_token) == []


def test_no_training_account_configured_means_no_writes(monkeypatch) -> None:
    monkeypatch.delenv(moysklad.SANDBOX_ENV, raising=False)
    mock = MoySkladMock(_state(TRAIN))
    creds = [{k: TRAIN[k] for k in ("id", "site", "username")}]
    result = run_write(PRODUCT, creds, lambda c: TRAIN["secret"], mock.transport())
    assert result.error == "write_forbidden" and mock.requests == []
    monkeypatch.setenv(moysklad.SANDBOX_ENV, "admin@heli-train, other")
    assert moysklad.sandbox_accounts() == {"heli-train", "other"}
    assert run_write(PRODUCT, creds, lambda c: TRAIN["secret"], mock.transport()).error is None


# --- границы записи -----------------------------------------------------------------------------


def test_only_post_to_the_four_collections() -> None:
    def never(request):  # pragma: no cover - must not be reached
        raise AssertionError(request.url)

    with Api("Basic x", httpx.MockTransport(never)) as api:
        for path in ("entity/product/00000000-0000-4000-8000-000000000401", "entity/store", "entity/counterparty",
                     "entity/supply/00000000-0000-4000-8000-000000000001/positions", "entity/product/delete"):
            with pytest.raises(MsError) as exc:
                api.post(path, {})
            assert exc.value.code == "write_forbidden"
    assert not hasattr(Api, "put") and not hasattr(Api, "delete")
    assert set(moysklad.WRITE_ACTIONS) == {"create_product", "create_supply", "create_customerorder", "create_invoiceout"}
    assert _write(MoySkladMock(_state(TRAIN)), {"action": "delete_product"}).error == "bad_action"


def test_a_mismatch_on_the_second_get_is_reported() -> None:
    mock = MoySkladMock(_state(TRAIN))

    def lying(request: httpx.Request) -> httpx.Response:
        response = mock.handle(request)
        if request.method == "GET" and request.url.path.count("/entity/product/") and mock.ms["products"][-1]["article"] == "GL-M-100":
            data = response.json()
            data["article"] = "GL-M-1OO"
            return httpx.Response(200, json=data)
        return response

    creds = [{k: TRAIN[k] for k in ("id", "site", "username")}]
    result = run_write(PRODUCT, creds, lambda c: TRAIN["secret"], httpx.MockTransport(lying), sandbox=SANDBOX)
    assert result.error == "verify_mismatch" and "НЕ совпало" in result.content and "article" in result.content


def test_api_errors_are_honest() -> None:
    mock = MoySkladMock(_state(TRAIN))
    result = _write(mock, {"action": "create_product", "name": ""})
    assert result.error == "bad_args" and _posts(mock) == []

    def down(request):
        raise httpx.ConnectError("down")

    creds = [{k: TRAIN[k] for k in ("id", "site", "username")}]
    result = run_write(PRODUCT, creds, lambda c: TRAIN["secret"], httpx.MockTransport(down), sandbox=SANDBOX)
    assert result.error == "api_unavailable" and "проверь через moysklad_read" in result.content


# --- исполнитель с настоящим хранилищем «Доступов» ---------------------------------------------


def _user(session) -> User:
    user = User(email="ms@example.com", consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def test_executor_writes_with_the_vault_and_never_logs_the_secret(session, monkeypatch, caplog) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv(moysklad.SANDBOX_ENV, "heli-train")
    user = _user(session)
    store_credential(session, user, TRAIN["site"], TRAIN["username"], TRAIN["secret"])
    store_credential(session, user, "romashka.bitrix24.ru", "webhook", "https://romashka.bitrix24.ru/rest/1/abc/")
    mock = MoySkladMock(_state(TRAIN))
    caplog.set_level(logging.DEBUG)
    result = moysklad.execute(session, user, None, "moysklad_write", PRODUCT, transport=mock.transport())
    assert result.error is None, result.content
    assert TRAIN["secret"] not in result.content and TRAIN["secret"] not in caplog.text
    assert base64.b64encode(f"{TRAIN['username']}:{TRAIN['secret']}".encode()).decode() not in caplog.text
    read = moysklad.execute(session, user, None, "moysklad_read", {"resource": "product", "filter": "article=GL-M-100"},
                            transport=mock.transport())
    assert read.error is None and "GL-M-100" in read.content


def test_find_credential_points_to_the_api(session, monkeypatch) -> None:
    from app.agent import credentials

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv(moysklad.SANDBOX_ENV, "heli-train")
    user = _user(session)
    store_credential(session, user, TRAIN["site"], TRAIN["username"], TRAIN["secret"])
    note = credentials.execute(session, user, None, "find_credential", {"site": "online.moysklad.ru"}).content
    assert "moysklad_write" in note and "heli-train" in note and TRAIN["secret"] not in note
    monkeypatch.setenv(moysklad.SANDBOX_ENV, "")
    note = credentials.execute(session, user, None, "find_credential", {"site": "online.moysklad.ru"}).content
    assert "боевой" in note and "отклонит" in note


def test_write_policy_rule_when_present(monkeypatch) -> None:
    if moysklad._write_policy is None:
        pytest.skip("write_policy (PR #61) is not merged yet")
    wp = moysklad._write_policy
    monkeypatch.setenv(moysklad.SANDBOX_ENV, "heli-train")
    assert wp.classify("moysklad_write", {**PRODUCT, "account": "heli-train"}).kind == wp.SANDBOX
    # Боевой аккаунт: карточки нет (подтверждать нечего), исполнитель всё равно откажет.
    assert not wp.needs_confirmation("moysklad_write", {**PRODUCT, "account": "romashka-trade"})
