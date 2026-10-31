import base64
import json
import logging

from sqlalchemy import select

from app.auth import create_invite
from app.models import Credential, CredentialAccess, User
from app.vault import VaultError, fill_for_model, secret_for_browser, store_credential
from datetime import datetime, timezone


SECRET = "unit-test-secret"
KEY = base64.b64encode(b"k" * 32).decode()


def _user(session, email="vault@example.com") -> User:
    user = User(
        email=email,
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def test_missing_key_refuses_to_store(session, monkeypatch) -> None:
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    user = _user(session)
    try:
        store_credential(session, user, "example.test", "ada", SECRET)
    except VaultError as exc:
        assert exc.code == "vault_unavailable"
    else:
        raise AssertionError("vault stored a secret without a key")


def test_secret_stays_out_of_the_model_and_the_logs(session, monkeypatch, caplog) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    caplog.set_level(logging.INFO)
    user = _user(session)
    row = store_credential(session, user, "example.test", "ada", SECRET)
    assert row.ciphertext != SECRET.encode()
    assert SECRET.encode() not in row.ciphertext
    placeholder = fill_for_model(session, user, row.id)
    assert placeholder == "<CREDENTIAL>"
    assert secret_for_browser(session, user, row.id) == SECRET
    actions = list(
        session.scalars(select(CredentialAccess.action).where(CredentialAccess.credential_id == row.id))
    )
    assert actions == ["create", "fill", "browser_fill"]
    if SECRET in caplog.text:
        raise AssertionError("secret appeared in logs")


def _login(client, session, email: str) -> dict[str, str]:
    invite = create_invite(session)
    session.commit()
    issued = client.post("/v1/auth/request-code", json={"email": email, "invite_code": invite})
    assert issued.status_code == 202
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": email, "code": code}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_http_never_returns_the_secret(client, session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    headers = _login(client, session, "safe@example.com")
    created = client.post(
        "/v1/credentials",
        headers=headers,
        json={"site": "example.test", "username": "ada", "secret": SECRET},
    )
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "site", "username"}
    assert SECRET not in json.dumps(body)
    listed = client.get("/v1/credentials", headers=headers)
    assert SECRET not in listed.text
    filled = client.post(f"/v1/credentials/{body['id']}/fill", headers=headers)
    assert filled.json() == {"placeholder": "<CREDENTIAL>"}
    assert SECRET not in filled.text
    stored = session.get(Credential, __import__("uuid").UUID(body["id"]))
    assert stored is not None
    assert SECRET.encode() not in stored.ciphertext
    removed = client.delete(f"/v1/credentials/{body['id']}", headers=headers)
    assert removed.status_code == 200
    assert client.get("/v1/credentials", headers=headers).json()["credentials"] == []


def test_http_reports_missing_vault(client, session, monkeypatch) -> None:
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    headers = _login(client, session, "nokey@example.com")
    created = client.post(
        "/v1/credentials",
        headers=headers,
        json={"site": "example.test", "username": "ada", "secret": SECRET},
    )
    assert created.status_code == 503
    assert created.json()["detail"] == "vault_unavailable"
    assert SECRET not in created.text
