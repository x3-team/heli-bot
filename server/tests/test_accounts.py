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
