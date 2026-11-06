"""The schedule ticker: due schedules run on their own, once, without Telegram."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import schedules
from app.browser import MemoryDriver
from app.llm.client import HeliError
from app.models import Schedule, Task
from app.schedules import _claim, autorun_enabled, create_schedule, install_ticker, tick
from tests.test_schedules import _heli, _Scripted, _user

# Monday 09.11.2026 09:01 МСК.
MONDAY_0901 = datetime(2026, 11, 9, 6, 1, 20, tzinfo=timezone.utc)
CLASSIFY = '{"task_type":"status","complexity":1}'


@pytest.fixture()
def factory(session):
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _due(session, user, *, minutes_late=1, label="Сводка", request="Кратко напомни про учебный ящик") -> Schedule:
    row = create_schedule(session, user, label, "0 9 * * 1", request, now=MONDAY_0901 - timedelta(days=1))
    row.next_run = MONDAY_0901.replace(second=0) - timedelta(minutes=minutes_late)
    session.commit()
    return row


def _reload(factory, schedule_id) -> Schedule:
    with factory() as fresh:
        return fresh.get(Schedule, schedule_id)


def test_due_schedule_runs_once_and_moves_to_next_week(session, factory, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(schedules, "notify", lambda text: sent.append(text))
    monkeypatch.delenv("HELI_SCHEDULE_TELEGRAM", raising=False)
    user = _user(session)
    row = _due(session, user)
    script = _Scripted([CLASSIFY, "Учебный ящик на месте, писем нет."])

    assert tick(factory, lambda: _heli(script), MemoryDriver, now=MONDAY_0901) == 1

    saved = _reload(factory, row.id)
    assert saved.last_result == "Учебный ящик на месте, писем нет."
    assert saved.last_error is None
    assert saved.next_run.replace(tzinfo=timezone.utc) == datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
    with factory() as fresh:
        task = fresh.scalars(select(Task).where(Task.user_id == user.id)).one()
    assert task.context["schedule_id"] == str(row.id)
    assert task.final_answer == "Учебный ящик на месте, писем нет."
    assert sent == []  # no Telegram by default

    again = _Scripted([])
    assert tick(factory, lambda: _heli(again), MemoryDriver, now=MONDAY_0901 + timedelta(seconds=30)) == 0
    assert again.models == []


def test_telegram_only_when_switched_on(session, factory, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(schedules, "notify", lambda text: sent.append(text))
    monkeypatch.setenv("HELI_SCHEDULE_TELEGRAM", "1")
    _due(session, _user(session))
    tick(factory, lambda: _heli(_Scripted([CLASSIFY, "Готово"])), MemoryDriver, now=MONDAY_0901)
    assert sent == ["Сводка: Готово"]


def test_a_late_run_is_skipped_not_replayed(session, factory) -> None:
    row = _due(session, _user(session), minutes_late=120)
    script = _Scripted([])
    assert tick(factory, lambda: _heli(script), MemoryDriver, now=MONDAY_0901) == 0
    assert script.models == []
    saved = _reload(factory, row.id)
    assert saved.last_error == "missed"
    assert saved.next_run.replace(tzinfo=timezone.utc) > MONDAY_0901


def test_two_servers_claim_one_run(session, factory) -> None:
    row = _due(session, _user(session))
    first, second = factory(), factory()
    try:
        mine = first.get(Schedule, row.id)
        theirs = second.get(Schedule, row.id)
        assert _claim(first, mine, MONDAY_0901)
        first.commit()
        assert not _claim(second, theirs, MONDAY_0901)
    finally:
        first.close()
        second.close()


def test_disabled_and_future_schedules_wait(session, factory) -> None:
    user = _user(session)
    off = _due(session, user, label="Выключено")
    off.enabled = False
    future = create_schedule(session, user, "Потом", "0 9 * * 1", "Позже", now=MONDAY_0901)
    session.commit()
    script = _Scripted([])
    assert tick(factory, lambda: _heli(script), MemoryDriver, now=MONDAY_0901) == 0
    assert script.models == []
    assert _reload(factory, future.id).last_run is None


def test_one_broken_run_does_not_stop_the_others(session, factory, monkeypatch) -> None:
    user = _user(session)
    broken = _due(session, user, label="Сломано", request="первая")
    fine = _due(session, user, label="Нормально", request="вторая")
    real = schedules.run_turn

    def run_turn(db, owner, task, client):
        if task.request_text == "первая":
            raise RuntimeError("boom")
        return real(db, owner, task, client)

    monkeypatch.setattr(schedules, "run_turn", run_turn)
    script = _Scripted([CLASSIFY, "Вторая готова"])
    assert tick(factory, lambda: _heli(script), MemoryDriver, now=MONDAY_0901) == 2
    failed = _reload(factory, broken.id)
    assert failed.last_error == "run_failed"
    assert failed.next_run.replace(tzinfo=timezone.utc) > MONDAY_0901  # not retried every minute
    assert _reload(factory, fine.id).last_result == "Вторая готова"


def test_without_api_key_nothing_is_claimed(session, factory) -> None:
    row = _due(session, _user(session))
    before = _reload(factory, row.id).next_run

    def no_key():
        raise HeliError("missing_api_key")

    assert tick(factory, no_key, MemoryDriver, now=MONDAY_0901) == 0
    assert _reload(factory, row.id).next_run == before


def test_ticker_is_off_in_tests_and_by_switch(monkeypatch) -> None:
    assert not autorun_enabled()
    assert install_ticker() is False
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("HELI_SCHEDULES_AUTORUN", "0")
    assert not autorun_enabled()
    monkeypatch.setenv("HELI_SCHEDULES_AUTORUN", "1")
    assert autorun_enabled()
