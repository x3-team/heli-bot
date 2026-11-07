"""Смена стадии сделки Битрикс24 (b24-04): стадия сверяется с воронкой сделки и перечитывается после записи.

Портал — MockTransport, который ведёт себя как Битрикс24: crm.deal.update отвечает true и на код стадии
из чужой воронки, но такую стадию не применяет. Хосты и ключи выдуманы.
"""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.agent import bitrix, write_policy
from app.models import User
from app.vault import store_credential

KEY = base64.b64encode(b"k" * 32).decode()
DEMO = "b24-lvsc4f.bitrix24.ru"
LIVE = "other.bitrix24.ru"
STAGES = {
    "DEAL_STAGE": [
        {"STATUS_ID": "NEW", "NAME": "Новая"},
        {"STATUS_ID": "PREPARATION", "NAME": "Подготовка документов"},
        {"STATUS_ID": "WON", "NAME": "Сделка успешна"},
    ],
    "DEAL_STAGE_3": [
        {"STATUS_ID": "C3:NEW", "NAME": "Новая"},
        {"STATUS_ID": "C3:PREPARATION", "NAME": "Подготовка документов"},
        {"STATUS_ID": "C3:EXECUTING", "NAME": "В работе"},
        {"STATUS_ID": "C3:WON", "NAME": "Сделка успешна"},
    ],
}


class Portal:
    def __init__(self, sticky: bool = False):
        self.deals = {"7": {"ID": "7", "CATEGORY_ID": "0", "STAGE_ID": "NEW"},
                      "9": {"ID": "9", "CATEGORY_ID": "3", "STAGE_ID": "C3:NEW"}}
        self.sticky = sticky  # робот или обязательные поля откатывают стадию
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "crm.deal.get":
            deal = self.deals.get(str(body.get("id")))
            if deal is None:
                return httpx.Response(400, json={"error": "", "error_description": "Not found"})
            return httpx.Response(200, json={"result": dict(deal)})
        if method == "crm.status.list":
            return httpx.Response(200, json={"result": STAGES.get(body["filter"]["ENTITY_ID"], [])})
        if method == "crm.deal.update":
            deal = self.deals[str(body.get("id") or body.get("ID"))]
            stage = body["fields"].get("STAGE_ID")
            if stage is None:
                return httpx.Response(200, json={"result": True})
            pipeline = "DEAL_STAGE" if deal["CATEGORY_ID"] == "0" else f"DEAL_STAGE_{deal['CATEGORY_ID']}"
            if not self.sticky and stage in {s["STATUS_ID"] for s in STAGES[pipeline]}:
                deal["STAGE_ID"] = stage
            return httpx.Response(200, json={"result": True})
        return httpx.Response(200, json={"result": {"task": {"id": "1"}}})

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def updates(self) -> list[dict]:
        return [b for m, b in self.calls if m == "crm.deal.update"]


@pytest.fixture
def user(session, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.delenv("HELI_BITRIX_SANDBOX_HOSTS", raising=False)
    u = User(email="stage@example.com", consent_pdn=True, consent_training=False, is_admin=False,
             created_at=datetime.now(timezone.utc))
    session.add(u)
    session.flush()
    store_credential(session, u, DEMO, "webhook", f"https://{DEMO}/rest/1/demosecret11/")
    store_credential(session, u, LIVE, "webhook", f"https://{LIVE}/rest/7/livesecret77/")
    session.flush()
    return u


def _write(session, user, portal, params, host=DEMO, **extra):
    args = {"method": "crm.deal.update", "params": params, "portal": host, **extra}
    return bitrix.execute(session, user, None, "bitrix24_write", args, transport=httpx.MockTransport(portal.handler))


def test_stage_change_in_the_main_pipeline_is_read_back(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"id": 7, "fields": {"STAGE_ID": "PREPARATION"}})
    assert res.error is None
    assert portal.deals["7"]["STAGE_ID"] == "PREPARATION"
    assert portal.methods() == ["crm.deal.get", "crm.status.list", "crm.deal.update", "crm.deal.get"]
    assert "NEW «Новая» → PREPARATION «Подготовка документов»" in res.content and "учебном" in res.content


def test_main_pipeline_code_on_a_deal_of_another_pipeline_is_mapped(session, user) -> None:
    """Корень b24-04: коды из ENTITY_ID=DEAL_STAGE на сделке воронки 3 Битрикс24 принимает и не применяет."""
    portal = Portal()
    res = _write(session, user, portal, {"id": "9", "fields": {"STAGE_ID": "PREPARATION"}})
    assert res.error is None
    assert portal.updates() == [{"id": "9", "fields": {"STAGE_ID": "C3:PREPARATION"}}]
    assert portal.deals["9"]["STAGE_ID"] == "C3:PREPARATION"
    assert ("crm.status.list", {"filter": {"ENTITY_ID": "DEAL_STAGE_3"}, "order": {"SORT": "ASC"}}) in portal.calls


def test_stage_by_name(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"ID": 9, "FIELDS": {"stage_id": "«в работе»", "COMMENTS": "звонок"}})
    assert res.error is None and portal.deals["9"]["STAGE_ID"] == "C3:EXECUTING"
    assert portal.updates() == [{"ID": 9, "fields": {"COMMENTS": "звонок", "STAGE_ID": "C3:EXECUTING"}}]


def test_unknown_stage_writes_nothing_and_lists_the_pipeline(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"id": 9, "fields": {"STAGE_ID": "Переговоры"}})
    assert res.error == "bad_stage" and portal.updates() == []
    assert "C3:EXECUTING — В работе" in res.content and "CATEGORY_ID=3" in res.content


def test_stage_rolled_back_by_the_portal_is_not_reported_as_done(session, user) -> None:
    portal = Portal(sticky=True)
    res = _write(session, user, portal, {"id": 7, "fields": {"STAGE_ID": "WON"}})
    assert res.error == "stage_not_changed"
    assert "не сменилась" in res.content and "Записано" not in res.content


def test_same_stage_is_not_written_again(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"id": 7, "fields": {"STAGE_ID": "NEW"}})
    assert res.error is None and portal.updates() == [] and "уже на стадии" in res.content


def test_missing_deal_id(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"fields": {"STAGE_ID": "WON"}})
    assert res.error == "bad_params" and portal.calls == []


def test_live_stage_change_still_needs_approval(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"id": 7, "fields": {"STAGE_ID": "WON"}}, host=LIVE)
    assert res.error == write_policy.REFUSED_CODE and portal.calls == []
    res = _write(session, user, portal, {"id": 7, "fields": {"STAGE_ID": "WON"}}, host=LIVE,
                 **{write_policy.CONFIRMED_ARG: True})
    assert res.error is None and portal.deals["7"]["STAGE_ID"] == "WON" and "боевом" in res.content


def test_other_deal_updates_keep_the_plain_path(session, user) -> None:
    portal = Portal()
    res = _write(session, user, portal, {"id": 7, "fields": {"TITLE": "Новое имя"}})
    assert res.error is None and portal.methods() == ["crm.deal.update"]
