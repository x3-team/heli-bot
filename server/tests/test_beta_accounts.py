"""Paid beta: own accounts by invite, the training account survives HELI_OPEN_ACCESS=0, per-user limits."""

import base64
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth import issue_token, training_user
from app.models import User
from app.quota import QuotaError, _ensure_room, record_charge, snapshot

KEY = base64.b64encode(b"k" * 32).decode()
PASSWORD = "long-enough-secret"


def _register(client, email: str, **extra):
    body = {"email": email, "password": PASSWORD, "consent_pdn": True, **extra}
    return client.post("/v1/auth/register", json=body)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def beta(client, monkeypatch):
    """Shared-server settings for testers: no open access, invite only, one admin."""
    monkeypatch.setenv("HELI_OPEN_ACCESS", "0")
    monkeypatch.setenv("HELI_INVITE_ONLY", "1")
    monkeypatch.setenv("HELI_ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    boss = _register(client, "boss@example.com")
    assert boss.status_code == 201, boss.text
    return _bearer(boss.json()["access_token"])


def _tester(client, admin: dict, email: str) -> dict:
    invite = client.post("/v1/admin/invites", json={}, headers=admin)
    assert invite.status_code == 201
    created = _register(client, email, invite_code=invite.json()["invite_code"])
    assert created.status_code == 201, created.text
    return _bearer(created.json()["access_token"])


def test_sign_up_needs_invite_admin_does_not(client, beta) -> None:
    assert client.get("/v1/me", headers=beta).json()["is_admin"] is True
    refused = _register(client, "stranger@example.com")
    assert refused.status_code == 403 and refused.json()["detail"] == "invite_required"
    assert client.post("/v1/auth/open").status_code == 404
    tester = _tester(client, beta, "tester@example.com")
    me = client.get("/v1/me", headers=tester).json()
    assert me["email"] == "tester@example.com" and me["is_admin"] is False
    # testers cannot hand out invites, codes or read the user list
    assert client.post("/v1/admin/invites", json={}, headers=tester).status_code == 403
    assert client.post("/v1/admin/codes", json={"email": "boss@example.com"}, headers=tester).status_code == 403
    assert client.get("/v1/admin/users", headers=tester).status_code == 403


def test_admin_login_and_reset_codes(client, beta) -> None:
    _tester(client, beta, "tester@example.com")
    login = client.post("/v1/admin/codes", json={"email": "tester@example.com"}, headers=beta)
    assert login.status_code == 201
    verified = client.post("/v1/auth/verify", json={"email": "tester@example.com", "code": login.json()["code"]})
    assert verified.status_code == 200
    # the code is single use
    again = client.post("/v1/auth/verify", json={"email": "tester@example.com", "code": login.json()["code"]})
    assert again.status_code == 401

    reset = client.post("/v1/admin/codes", json={"email": "tester@example.com", "purpose": "reset"}, headers=beta)
    done = client.post(
        "/v1/auth/recover/reset",
        json={"email": "tester@example.com", "code": reset.json()["code"], "password": "new-password-1"},
    )
    assert done.status_code == 200
    assert client.post("/v1/auth/login", json={"email": "tester@example.com", "password": PASSWORD}).status_code == 401
    ok = client.post("/v1/auth/login", json={"email": "tester@example.com", "password": "new-password-1"})
    assert ok.status_code == 200


def test_two_testers_do_not_see_each_other(client, beta) -> None:
    alice = _tester(client, beta, "alice@example.com")
    bob = _tester(client, beta, "bob@example.com")

    stored = client.post(
        "/v1/credentials", headers=alice, json={"site": "b24.example", "username": "alice", "secret": "s3cret-a"}
    )
    assert stored.status_code == 201, stored.text
    cred_id = stored.json()["id"]
    assert [c["username"] for c in client.get("/v1/credentials", headers=alice).json()["credentials"]] == ["alice"]
    assert client.get("/v1/credentials", headers=bob).json()["credentials"] == []
    assert client.post(f"/v1/credentials/{cred_id}/fill", headers=bob).status_code == 404
    assert client.delete(f"/v1/credentials/{cred_id}", headers=bob).status_code == 404

    task = client.post("/v1/tasks", headers=alice, json={"request": "Сверь сделки", "context": {}})
    assert task.status_code == 201
    task_id = task.json()["id"]
    step = {"thought": "x", "model_alias": "heli-fast", "observation_type": "dom", "observation_text": "чужое"}
    assert client.post(f"/v1/tasks/{task_id}/steps", headers=bob, json=step).status_code == 404
    assert client.post(f"/v1/tasks/{task_id}/steps", headers=alice, json=step).status_code == 201
    finished = client.post(f"/v1/tasks/{task_id}/finish", headers=bob, json={"outcome": "success"})
    assert finished.status_code == 404
    rated = client.post(f"/v1/tasks/{task_id}/feedback", headers=bob, json={"rating": 1, "acceptance": "accepted"})
    assert rated.status_code == 404


def test_old_shared_tokens_stop_working_and_are_never_admin(client, session, monkeypatch) -> None:
    monkeypatch.setenv("HELI_OPEN_ACCESS", "1")
    monkeypatch.setenv("HELI_ADMIN_EMAILS", "owner@heli.local")
    monkeypatch.setenv("HELI_TRAINING_EMAILS", "owner@heli.local")
    shared = _bearer(client.post("/v1/auth/open").json()["access_token"])
    assert client.get("/v1/me", headers=shared).json()["is_admin"] is False
    owner = session.scalar(select(User).where(User.email == "owner@heli.local"))
    legacy = _bearer(_legacy_token(owner.id))  # issued before tokens carried `via`

    monkeypatch.setenv("HELI_OPEN_ACCESS", "0")
    assert client.get("/v1/me", headers=shared).status_code == 401
    assert client.get("/v1/me", headers=legacy).status_code == 401
    assert client.post("/v1/auth/open").status_code == 404


def _legacy_token(user_id) -> str:
    import jwt

    from app.config import get_settings

    payload = {"sub": str(user_id), "exp": datetime(2099, 1, 1, tzinfo=timezone.utc)}
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def test_training_account_keeps_vault_tasks_and_quota_after_open_access_off(client, session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("HELI_OPEN_ACCESS", "1")
    shared = _bearer(client.post("/v1/auth/open").json()["access_token"])
    owner_id = client.get("/v1/me", headers=shared).json()["id"]
    webhook = client.post(
        "/v1/credentials",
        headers=shared,
        json={"site": "b24-lvsc4f.bitrix24.ru", "username": "webhook", "secret": "rest/1/training-hook"},
    )
    assert webhook.status_code == 201
    task = client.post("/v1/tasks", headers=shared, json={"request": "Учебный пакет", "context": {}})
    assert task.status_code == 201
    owner = session.get(User, __import__("uuid").UUID(owner_id))
    record_charge(session, owner, Decimal("40"))
    session.commit()
    week_before = client.get("/v1/usage", headers=shared).json()["week_percent"]
    assert week_before > 0

    # beta deploy: open access off, owner@heli.local becomes the training account
    monkeypatch.setenv("HELI_OPEN_ACCESS", "0")
    monkeypatch.setenv("HELI_INVITE_ONLY", "1")
    monkeypatch.setenv("HELI_ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("HELI_TRAINING_EMAILS", "owner@heli.local")
    assert client.get("/v1/me", headers=shared).status_code == 401

    # path 1: the admin issues a login code for the training account
    boss = _bearer(_register(client, "boss@example.com").json()["access_token"])
    users = {u["email"]: u for u in client.get("/v1/admin/users", headers=boss).json()["users"]}
    assert users["owner@heli.local"]["is_training"] is True
    code = client.post("/v1/admin/codes", json={"email": "owner@heli.local"}, headers=boss).json()["code"]
    fresh = client.post("/v1/auth/verify", json={"email": "owner@heli.local", "code": code})
    assert fresh.status_code == 200
    training = _bearer(fresh.json()["access_token"])
    me = client.get("/v1/me", headers=training).json()
    assert me["id"] == owner_id and me["is_admin"] is False
    creds = client.get("/v1/credentials", headers=training).json()["credentials"]
    assert [(c["site"], c["username"]) for c in creds] == [("b24-lvsc4f.bitrix24.ru", "webhook")]
    assert client.get("/v1/usage", headers=training).json()["week_percent"] == week_before
    step = {"thought": "x", "model_alias": "heli-fast", "observation_type": "dom", "observation_text": "ok"}
    assert client.post(f"/v1/tasks/{task.json()['id']}/steps", headers=training, json=step).status_code == 201

    # path 2: CLI token for the test packs (same account, not an admin)
    cli_token = issue_token(training_user(session, "owner@heli.local").id, via="training")
    session.commit()
    assert client.get("/v1/me", headers=_bearer(cli_token)).json()["id"] == owner_id

    # without HELI_TRAINING_EMAILS the shared account stays closed even with a fresh token
    monkeypatch.setenv("HELI_TRAINING_EMAILS", "")
    assert client.get("/v1/me", headers=training).status_code == 401


def test_training_token_only_for_listed_emails(session, monkeypatch) -> None:
    from app.auth import AuthError

    monkeypatch.setenv("HELI_TRAINING_EMAILS", "train@heli.local")
    with pytest.raises(AuthError):
        training_user(session, "tester@example.com")
    created = training_user(session, "Train@heli.local")
    assert created.email == "train@heli.local" and created.is_admin is False


def _plain(session, email: str) -> User:
    user = User(email=email, consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def test_daily_limit_is_per_user(session, monkeypatch) -> None:
    monkeypatch.setenv("DAILY_LIMIT_RUB", "100")
    training = _plain(session, "owner@heli.local")
    tester = _plain(session, "tester@example.com")
    record_charge(session, training, Decimal("100"))
    assert snapshot(session, training).blocked is True
    with pytest.raises(QuotaError):
        _ensure_room(session, training, Decimal("1"))
    other = snapshot(session, tester)
    assert other.blocked is False and other.daily_used == Decimal("0")
    _ensure_room(session, tester, Decimal("50"))  # no limit_reached for the second user


def test_global_daily_cap_is_opt_in(session, monkeypatch) -> None:
    monkeypatch.setenv("DAILY_LIMIT_RUB", "1000")
    first = _plain(session, "a@example.com")
    second = _plain(session, "b@example.com")
    record_charge(session, first, Decimal("200"))
    assert snapshot(session, second).blocked is False  # off by default
    monkeypatch.setenv("GLOBAL_DAILY_LIMIT_RUB", "200")
    assert snapshot(session, second).blocked is True
    with pytest.raises(QuotaError):
        _ensure_room(session, second, Decimal("1"))


def test_week_and_month_are_per_user(session) -> None:
    heavy = _plain(session, "heavy@example.com")
    light = _plain(session, "light@example.com")
    record_charge(session, heavy, Decimal("400"))  # over the default weekly 312.5
    assert snapshot(session, heavy).blocked is True
    assert snapshot(session, light).week_used == Decimal("0")
    assert snapshot(session, light).blocked is False


def test_admin_sets_weekly_and_monthly_per_user(client, beta) -> None:
    tester = _tester(client, beta, "tester@example.com")
    users = {u["email"]: u for u in client.get("/v1/admin/users", headers=beta).json()["users"]}
    target = users["tester@example.com"]
    assert Decimal(target["monthly_limit_rub"]) == Decimal("1000")  # DEFAULT_MONTHLY_LIMIT_RUB
    assert Decimal(target["weekly_limit_rub"]) == Decimal("312.5")

    changed = client.patch(f"/v1/admin/users/{target['id']}/limit", json={"weekly_limit_rub": 50}, headers=beta)
    assert changed.status_code == 200, changed.text
    assert Decimal(changed.json()["weekly_limit_rub"]) == Decimal("50")
    assert Decimal(changed.json()["monthly_limit_rub"]) == Decimal("1000")
    changed = client.patch(
        f"/v1/admin/users/{target['id']}/limit", json={"monthly_limit_rub": 3000, "weekly_limit_rub": 0}, headers=beta
    )
    assert Decimal(changed.json()["monthly_limit_rub"]) == Decimal("3000")
    assert Decimal(changed.json()["weekly_limit_rub"]) == Decimal("937.5")  # back to monthly/4×1.25
    assert client.patch(f"/v1/admin/users/{target['id']}/limit", json={"weekly_limit_rub": 1}, headers=tester).status_code == 403
    # rubles stay hidden from the tester
    assert "limit" not in "".join(client.get("/v1/usage", headers=tester).json().keys())
