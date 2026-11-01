import logging

from app.auth import hash_secret


def test_register_login_and_trial_usage(client, session) -> None:
    reg = client.post(
        "/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "long-enough-secret",
            "consent_pdn": True,
            "consent_training": False,
        },
    )
    assert reg.status_code == 201
    body = reg.json()
    assert body["trial_active"] is True
    token = body["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    usage = client.get("/v1/usage", headers=headers)
    assert usage.status_code == 200
    view = usage.json()
    assert "week_percent" in view
    assert view.get("trial_active") is True
    assert view.get("trial_ends_at")


def test_expired_trial_is_reported(client, session) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models import User

    reg = client.post(
        "/v1/auth/register",
        json={"email": "old@example.com", "password": "long-enough-secret", "consent_pdn": True},
    )
    user = session.scalar(select(User).where(User.email == "old@example.com"))
    user.trial_ends_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.flush()
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    assert client.get("/v1/usage", headers=headers).json()["trial_active"] is False
    login = client.post("/v1/auth/login", json={"email": "old@example.com", "password": "long-enough-secret"})
    assert login.json()["trial_active"] is False


def test_login_password_and_recovery(client) -> None:
    client.post(
        "/v1/auth/register",
        json={
            "email": "recover@example.com",
            "password": "first-password1",
            "consent_pdn": True,
            "consent_training": False,
        },
    )
    bad = client.post(
        "/v1/auth/login",
        json={"email": "recover@example.com", "password": "wrong-password"},
    )
    assert bad.status_code == 401
    ok = client.post(
        "/v1/auth/login",
        json={"email": "recover@example.com", "password": "first-password1"},
    )
    assert ok.status_code == 200

    req = client.post("/v1/auth/recover/request", json={"email": "recover@example.com"})
    assert req.status_code == 202
    code = client.app.state.mail_sink[-1]
    reset = client.post(
        "/v1/auth/recover/reset",
        json={"email": "recover@example.com", "code": code, "password": "second-password2"},
    )
    assert reset.status_code == 200
    assert reset.json()["trial_active"] is True
    login = client.post(
        "/v1/auth/login",
        json={"email": "recover@example.com", "password": "second-password2"},
    )
    assert login.status_code == 200


def test_code_login_without_invite(client) -> None:
    client.post(
        "/v1/auth/register",
        json={
            "email": "code@example.com",
            "password": "password-1234",
            "consent_pdn": True,
            "consent_training": False,
        },
    )
    issued = client.post("/v1/auth/request-code", json={"email": "code@example.com"})
    assert issued.status_code == 202
    code = client.app.state.mail_sink[-1]
    verified = client.post("/v1/auth/verify", json={"email": "code@example.com", "code": code})
    assert verified.status_code == 200


def test_passwords_not_logged_in_auth_flow(caplog, client) -> None:
    secret = "super-secret-password-99"
    caplog.set_level(logging.INFO)
    client.post(
        "/v1/auth/register",
        json={
            "email": "log@example.com",
            "password": secret,
            "consent_pdn": True,
            "consent_training": False,
        },
    )
    client.post("/v1/auth/login", json={"email": "log@example.com", "password": secret})
    assert secret not in caplog.text
    digest = hash_secret(secret)
    assert digest not in caplog.text


def test_open_access_is_off_by_default(client) -> None:
    assert client.post("/v1/auth/open").status_code == 404


def test_open_access_reuses_one_account(client, monkeypatch) -> None:
    monkeypatch.setenv("HELI_OPEN_ACCESS", "1")
    first = client.post("/v1/auth/open")
    assert first.status_code == 200
    assert first.json()["trial_active"] is True
    second = client.post("/v1/auth/open")
    me1 = client.get("/v1/me", headers={"Authorization": f"Bearer {first.json()['access_token']}"}).json()
    me2 = client.get("/v1/me", headers={"Authorization": f"Bearer {second.json()['access_token']}"}).json()
    assert me1["id"] == me2["id"]
    assert me1["consent_pdn"] is True


def test_open_access_account_reads_feedback_and_reports(client, monkeypatch) -> None:
    monkeypatch.setenv("HELI_OPEN_ACCESS", "1")
    token = client.post("/v1/auth/open").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    task = client.post("/v1/tasks", json={"request": "Найди аренду видеокарты", "context": {}}, headers=headers)
    assert task.status_code in (200, 201), task.text
    task_id = task.json()["id"]
    rated = client.post(
        f"/v1/tasks/{task_id}/feedback",
        json={"rating": 1, "acceptance": "accepted", "comment": "ссылки не кликабельны"},
        headers=headers,
    )
    assert rated.status_code == 200, rated.text
    report = client.post("/v1/reports/incident", json={"message": "Завис", "log_excerpt": "x"}, headers=headers)
    assert report.status_code == 201

    feedback = client.get("/v1/admin/feedback?rated_only=true", headers=headers)
    assert feedback.status_code == 200, feedback.text
    body = feedback.json()
    assert body["totals"]["rated"] == 1 and body["totals"]["thumbs_up"] == 1
    assert body["tasks"][0]["feedback"]["comment"] == "ссылки не кликабельны"
    incidents = client.get("/v1/admin/incidents", headers=headers).json()
    assert incidents["reports"][0]["message"] == "Завис"


def test_admin_feedback_needs_admin(client, session) -> None:
    from datetime import datetime, timezone

    from app.auth import issue_token
    from app.models import User

    user = User(email="plain@example.com", consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.commit()
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    assert client.get("/v1/admin/feedback", headers=headers).status_code == 403
    assert client.get("/v1/admin/incidents", headers=headers).status_code == 403
