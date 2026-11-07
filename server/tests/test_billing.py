from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import billing
from app.models import UsageLedger, User, WalletTx
from app.quota import QuotaError, _ensure_room, record_charge, snapshot
from app.schedules import ScheduleError, create_schedule

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


def _user(session, email="wallet@example.com", **extra) -> User:
    user = User(email=email, consent_pdn=True, created_at=NOW, **extra)
    session.add(user)
    session.flush()
    return user


def _signup(client, email="new@example.com") -> dict:
    response = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "password-123", "consent_pdn": True},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_wallet_never_negative_and_idempotent(session):
    user = _user(session)
    billing.post_tx(session, user, "topup", Decimal("100"), "Пополнение", ref="pay-1")
    billing.post_tx(session, user, "topup", Decimal("100"), "Пополнение", ref="pay-1")
    assert billing.balance(user) == Decimal("100.00")
    with pytest.raises(billing.BillingError) as err:
        billing.post_tx(session, user, "adjust", Decimal("-150"), "x")
    assert err.value.code == "insufficient_funds"
    billing.post_tx(session, user, "overage", Decimal("-150"), "x", allow_partial=True)
    assert billing.balance(user) == Decimal("0.00")


def test_buy_plan_and_switch_refunds_unused_part(session):
    user = _user(session)
    billing.post_tx(session, user, "topup", Decimal("2000"), "Пополнение")
    with pytest.raises(billing.BillingError):
        billing.buy_plan(session, user, "trial", now=NOW)
    billing.buy_plan(session, user, "start", now=NOW)
    assert billing.balance(user) == Decimal("1610.00")
    assert billing.plan_status(user, NOW) == "active"
    # Halfway through the period: half of 390 comes back, then Бизнес is paid.
    billing.buy_plan(session, user, "business", now=NOW + timedelta(days=15))
    assert billing.balance(user) == Decimal("1610") + Decimal("195") - Decimal("1490")
    assert user.plan == "business"
    with pytest.raises(billing.BillingError) as err:
        billing.buy_plan(session, user, "pro", now=NOW + timedelta(days=16))
    assert err.value.code == "insufficient_funds"


def test_plan_limits_replace_old_limits(session):
    user = _user(session, monthly_limit_rub=Decimal("1000"))
    billing.post_tx(session, user, "topup", Decimal("390"), "Пополнение")
    billing.buy_plan(session, user, "start", now=NOW)
    snap = snapshot(session, user, NOW)
    assert snap.monthly_limit == Decimal("120")
    assert snap.daily_limit == Decimal("15")
    record_charge(session, user, Decimal("15"), at=NOW + timedelta(minutes=1))
    assert snapshot(session, user, NOW + timedelta(minutes=2)).blocked  # day limit


def test_expired_plan_renews_from_wallet_or_requires_plan(session):
    user = _user(session)
    billing.post_tx(session, user, "topup", Decimal("400"), "Пополнение")
    billing.buy_plan(session, user, "start", now=datetime.now(timezone.utc) - timedelta(days=31))
    user.plan_auto_renew = False
    with pytest.raises(QuotaError) as err:
        _ensure_room(session, user, Decimal("0.1"))
    assert err.value.code == "plan_required"
    billing.post_tx(session, user, "topup", Decimal("380"), "Пополнение")
    user.plan_auto_renew = True
    _ensure_room(session, user, Decimal("0.1"))
    assert billing.plan_status(user) == "active"
    assert billing.balance(user) == Decimal("0.00")  # 400 - 390 + 380 - 390


def test_overage_debits_wallet_with_markup_up_to_cap(session):
    user = _user(session)
    billing.post_tx(session, user, "topup", Decimal("1000"), "Пополнение")
    billing.buy_plan(session, user, "start", now=NOW)
    start = NOW + timedelta(minutes=1)
    for day in range(8):
        record_charge(session, user, Decimal("15"), at=start + timedelta(days=day))
    # 120 ₽ spent = month limit. Overage off: blocked.
    later = start + timedelta(days=9)
    assert snapshot(session, user, later).blocked
    user.overage_enabled = True
    user.overage_cap_rub = Decimal("30")
    assert not snapshot(session, user, later).blocked
    before = billing.balance(user)
    record_charge(session, user, Decimal("10"), at=later)
    assert billing.balance(user) == before - Decimal("15.00")
    # Cap 30: the next 20 ₽ of cost would need 30 ₽, only 15 left → partial, then blocked.
    record_charge(session, user, Decimal("12"), at=later + timedelta(hours=1))
    assert billing.balance(user) == before - Decimal("30.00")
    assert snapshot(session, user, later + timedelta(hours=2)).blocked
    kinds = [row.kind for row in session.query(WalletTx).filter_by(user_id=user.id)]
    assert kinds.count("overage") == 2


def test_no_plan_keeps_old_behaviour(session):
    user = _user(session, monthly_limit_rub=Decimal("1000"))
    snap = snapshot(session, user, NOW)
    assert snap.monthly_limit == Decimal("1000")
    assert not snap.plan_expired
    record_charge(session, user, Decimal("5"), at=NOW)
    assert session.query(WalletTx).count() == 0
    assert session.query(UsageLedger).count() == 1


def test_schedule_limit_by_plan(session):
    user = _user(session)
    billing.post_tx(session, user, "topup", Decimal("390"), "Пополнение")
    billing.buy_plan(session, user, "start", now=NOW)
    create_schedule(session, user, "a", "0 9 * * *", "сводка", now=NOW)
    with pytest.raises(ScheduleError) as err:
        create_schedule(session, user, "b", "0 10 * * *", "сводка", now=NOW)
    assert err.value.code == "schedule_limit"


def test_billing_api_flow(client, session, monkeypatch):
    monkeypatch.setenv("HELI_BILLING", "1")
    monkeypatch.setenv("HELI_ADMIN_EMAILS", "admin@example.com")
    headers = _signup(client)
    view = client.get("/v1/billing", headers=headers).json()
    assert view["plan"]["status"] == "trial"
    assert view["wallet"]["balance_rub"] == "0.00"
    assert [plan["id"] for plan in view["plans"]] == ["start", "business", "pro"]
    assert "month_limit_rub" not in str(view)
    assert client.post("/v1/billing/plan", json={"plan": "start"}, headers=headers).status_code == 402
    assert client.post("/v1/billing/topup", json={"amount_rub": 500}, headers=headers).status_code == 501

    admin = _signup(client, "admin@example.com")
    target = session.query(User).filter_by(email="new@example.com").one()
    credited = client.post(
        f"/v1/admin/users/{target.id}/wallet",
        json={"amount_rub": 500, "note": "Счёт 12", "ref": "inv-12"},
        headers=admin,
    )
    assert credited.status_code == 200, credited.text
    client.post(f"/v1/admin/users/{target.id}/wallet", json={"amount_rub": 500, "ref": "inv-12"}, headers=admin)
    assert client.post(f"/v1/admin/users/{target.id}/wallet", json={"amount_rub": 5}, headers=headers).status_code == 403

    bought = client.post("/v1/billing/plan", json={"plan": "start"}, headers=headers).json()
    assert bought["plan"]["id"] == "start"
    assert bought["wallet"]["balance_rub"] == "110.00"
    settings = client.put(
        "/v1/billing/settings", json={"overage_enabled": True, "overage_cap_rub": 200}, headers=headers
    ).json()
    assert settings["overage"] == {"enabled": True, "cap_rub": "200.00", "spent_rub": "0.00", "typical_task_rub": "2.10"}
    items = client.get("/v1/billing/history", headers=headers).json()["items"]
    assert [item["kind"] for item in items] == ["plan", "topup"]
    assert items[0]["amount_rub"] == "-390.00"


def test_signup_without_billing_flag_has_no_plan(client):
    headers = _signup(client, "beta@example.com")
    view = client.get("/v1/billing", headers=headers).json()
    assert view["plan"]["status"] == "none"
    assert view["plan"]["title"] == "Бета-тест"


def test_overage_covers_day_limit_too(session):
    user = _user(session)
    billing.post_tx(session, user, "topup", Decimal("500"), "Пополнение")
    billing.buy_plan(session, user, "start", now=NOW)
    at = NOW + timedelta(minutes=1)
    record_charge(session, user, Decimal("15"), at=at)
    assert snapshot(session, user, at).blocked
    user.overage_enabled = True
    user.overage_cap_rub = Decimal("100")
    assert not snapshot(session, user, at).blocked
    before = billing.balance(user)
    record_charge(session, user, Decimal("2"), at=at + timedelta(minutes=1))
    assert billing.balance(user) == before - Decimal("3.00")


def test_plan_caps_task_and_smart_model(session):
    user = _user(session)
    assert billing.task_caps(user) == (None, None, None)
    assert billing.smart_allowed(user)
    billing.post_tx(session, user, "topup", Decimal("390"), "Пополнение")
    billing.buy_plan(session, user, "start", now=NOW)
    assert billing.task_caps(user) == (30, 0, Decimal("10"))
    assert not billing.smart_allowed(user)
    assert billing.tasks_left(Decimal("120")) == 85
