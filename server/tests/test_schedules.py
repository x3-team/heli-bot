import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.auth import create_invite
from app.browser import MemoryDriver
from app.llm.client import HeliClient
from app.models import UsageLedger, User
from app.notify import notify
from app.schedules import create_schedule, next_after, parse_expression, run_due
from sqlalchemy import func, select


class _Scripted:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.models: list[str] = []

    def create(self, **kwargs):
        self.models.append(kwargs["model"])
        text = self.texts.pop(0)

        class Message:
            content = text

        class Choice:
            message = Message()

        class Usage:
            prompt_tokens = 10
            completion_tokens = 4

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


def _user(session) -> User:
    user = User(
        email="schedules@example.com",
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def _heli(script: _Scripted) -> HeliClient:
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    return HeliClient(sdk)


def test_monday_phrase_is_nine_moscow(session) -> None:
    assert parse_expression("каждый понедельник в 9:00") == "0 9 * * 1"
    sunday = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    nxt = next_after("0 9 * * 1", sunday)
    assert nxt == datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)


def test_due_run_spends_quota_and_skips_notification_without_telegram(session, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    user = _user(session)
    row = create_schedule(session, user, "Отчёт", "0 9 * * 1", "Собери статус")
    row.next_run = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.flush()
    script = _Scripted(['{"task_type":"status","complexity":1}', "Статус готов"])
    ran = run_due(session, _heli(script), MemoryDriver(), user_id=user.id, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert ran == 1
    assert row.last_result == "Статус готов"
    assert row.last_error is None
    spent = session.scalar(select(func.count()).select_from(UsageLedger).where(UsageLedger.user_id == user.id))
    assert spent == 2
    assert notify("проверка") is False
    assert "notification skipped" in caplog.text


def test_due_run_stops_when_the_quota_is_full(session) -> None:
    user = _user(session)
    user.weekly_limit_rub = Decimal("0")
    session.flush()
    row = create_schedule(session, user, "Отчёт", "0 9 * * 1", "Собери статус")
    row.next_run = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.flush()
    script = _Scripted(["не вызывать"])
    run_due(session, _heli(script), MemoryDriver(), user_id=user.id)
    assert script.models == []
    assert row.last_error == "limit_reached"


def test_schedule_http(client, session) -> None:
    invite = create_invite(session)
    session.commit()
    issued = client.post(
        "/v1/auth/request-code",
        json={"email": "when@example.com", "invite_code": invite},
    )
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": "when@example.com", "code": code}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/v1/schedules",
        headers=headers,
        json={"label": "Планёрка", "expression": "каждый понедельник в 9:00", "request": "Собери статус"},
    )
    assert created.status_code == 201
    assert created.json()["expression"] == "0 9 * * 1"
    listed = client.get("/v1/schedules", headers=headers).json()["schedules"]
    assert listed[0]["label"] == "Планёрка"
    removed = client.delete(f"/v1/schedules/{created.json()['id']}", headers=headers)
    assert removed.status_code == 200
    assert client.get("/v1/schedules", headers=headers).json()["schedules"] == []
