import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api import get_heli_client
from app.auth import create_invite
from app.llm.client import HeliClient
from app.llm.run import run_turn
from app.models import QuotaAlert, User
from app.quota import QuotaError, metered_complete, public_usage, record_charge, snapshot, weekly_limit_of
from app.trajectories import start_task


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


def _user(session, email="quota@example.com") -> User:
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


def _client(script: _Scripted) -> HeliClient:
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    return HeliClient(sdk)


def test_weekly_limit_is_monthly_over_four_times_1_25(session) -> None:
    user = _user(session)
    assert user.monthly_limit_rub == Decimal("1000.000000") or Decimal(user.monthly_limit_rub) == Decimal("1000")
    assert weekly_limit_of(user) == Decimal("312.500000")


def test_warns_at_80_and_stops_at_100(session) -> None:
    user = _user(session)
    record_charge(session, user, Decimal("250"))
    warned = snapshot(session, user)
    assert warned.week_percent == Decimal("80.00")
    assert warned.warning is True
    assert warned.blocked is False
    view = public_usage(warned)
    assert set(view) == {"week_percent", "month_percent", "blocked", "warning", "reset_label", "trial_active"}
    assert "rub" not in json.dumps(view)

    record_charge(session, user, Decimal("62.5"))
    stopped = snapshot(session, user)
    assert stopped.week_percent == Decimal("100.00")
    assert stopped.blocked is True
    script = _Scripted(['{"task_type":"status","complexity":1}', "ok"])
    task = start_task(session, user, "статус", {})
    with pytest.raises(QuotaError) as caught:
        run_turn(session, user, task, _client(script))
    assert caught.value.code == "limit_reached"
    assert script.models == []


def test_reserve_stops_before_the_call_when_estimate_crosses_100(session) -> None:
    user = _user(session)
    user.weekly_limit_rub = Decimal("1")
    session.flush()
    record_charge(session, user, Decimal("0.999"))
    script = _Scripted(["не должен вызваться"])
    with pytest.raises(QuotaError) as caught:
        metered_complete(
            session,
            user,
            None,
            _client(script),
            "heli-fast",
            [{"role": "user", "content": "задача"}],
            max_tokens=100_000,
        )
    assert caught.value.code == "limit_reached"
    assert script.models == []


def test_calendar_week_resets_monday_moscow(session) -> None:
    user = _user(session)
    monday = datetime(2026, 10, 25, 21, 30, tzinfo=timezone.utc)
    sunday = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)
    record_charge(session, user, Decimal("10"), at=sunday)
    record_charge(session, user, Decimal("5"), at=monday)
    snap = snapshot(session, user, monday)
    assert snap.week_used == Decimal("5.000000")
    assert snap.month_used == Decimal("15.000000")
    assert snap.week_key == "2026-10-26"


def test_alerts_at_50_80_and_95(session) -> None:
    user = _user(session)
    record_charge(session, user, Decimal("296.875"))
    rows = list(
        session.scalars(
            select(QuotaAlert).where(QuotaAlert.user_id == user.id, QuotaAlert.window == "week")
        )
    )
    assert sorted(row.threshold for row in rows) == [50, 80, 95]
    record_charge(session, user, Decimal("0"))
    again = session.scalar(
        select(func.count()).select_from(QuotaAlert).where(QuotaAlert.user_id == user.id, QuotaAlert.window == "week")
    )
    assert again == 3


def test_daily_fuse_stops_everyone(session, monkeypatch) -> None:
    monkeypatch.setenv("DAILY_LIMIT_RUB", "1")
    user = _user(session)
    other = _user(session, "other@example.com")
    record_charge(session, other, Decimal("1"))
    snap = snapshot(session, user)
    assert snap.blocked is True
    assert snap.week_percent == Decimal("0.00")
    script = _Scripted(["нет"])
    with pytest.raises(QuotaError):
        metered_complete(
            session,
            user,
            None,
            _client(script),
            "heli-fast",
            [{"role": "user", "content": "задача"}],
            max_tokens=16,
        )
    assert script.models == []


def test_usage_endpoint_hides_rubles_and_run_returns_limit(client, session) -> None:
    user_invite = create_invite(session)
    session.commit()
    issued = client.post(
        "/v1/auth/request-code",
        json={"email": "limit@example.com", "invite_code": user_invite},
    )
    assert issued.status_code == 202
    code = client.app.state.mail_sink[-1]
    token = client.post(
        "/v1/auth/verify",
        json={"email": "limit@example.com", "code": code},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.put("/v1/me/consents", headers=headers, json={"consent_pdn": True, "consent_training": False})
    from app.models import User as UserModel

    person = session.scalar(select(UserModel).where(UserModel.email == "limit@example.com"))
    person.weekly_limit_rub = Decimal("1")
    record_charge(session, person, Decimal("0.8"))
    session.commit()
    body = client.get("/v1/usage", headers=headers)
    assert body.status_code == 200
    payload = body.json()
    assert payload["week_percent"] == 80
    assert payload["warning"] is True
    assert payload["blocked"] is False
    raw = json.dumps(payload)
    assert "rub" not in raw
    assert "₽" not in raw

    record_charge(session, person, Decimal("0.2"))
    session.commit()
    created = client.post("/v1/tasks", headers=headers, json={"request": "статус", "context": {}})
    assert created.status_code == 201
    called: list[str] = []

    class _Idle:
        def complete(self, *args, **kwargs):
            called.append("called")
            raise AssertionError("model called")

    client.app.dependency_overrides[get_heli_client] = lambda: _Idle()
    run = client.post(f"/v1/tasks/{created.json()['id']}/run", headers=headers)
    assert run.status_code == 403
    assert run.json()["detail"] == "limit_reached"
    assert called == []
