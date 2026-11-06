"""schedule_create: a recurring task for the assistant itself is a Heli schedule, not a CRM task."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.agent import schedule_tool
from app.agent.executors import executor_for
from app.agent.task_run import system_prompt
from app.agent.tools import client_tool_names
from app.models import Schedule, User
from app.schedules import describe, next_after
from app.trajectories import start_task
from evals.world import World


def _user(session, email="sched-tool@example.com") -> User:
    user = User(email=email, consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def _create(session, user, task_id=None, **args):
    payload = {"label": "Сводка по учебному ящику", "expression": "0 9 * * 1",
               "request": "Кратко напомни про учебный ящик, ничего не отправляй.", **args}
    return schedule_tool.execute(session, user, task_id, "schedule_create", payload)


def _count(session, user) -> int:
    return session.scalar(select(func.count()).select_from(Schedule).where(Schedule.user_id == user.id))


@pytest.mark.parametrize(
    "cron, words",
    [
        ("0 9 * * 1", "каждый понедельник в 9:00 МСК"),
        ("30 18 * * *", "каждый день в 18:30 МСК"),
        ("0 17 * * 5", "каждую пятницу в 17:00 МСК"),
        ("0 10 * * 0", "каждое воскресенье в 10:00 МСК"),
        ("0 10 * * 7", "каждое воскресенье в 10:00 МСК"),
        ("0 10 1 * *", "каждый месяц 1-го числа в 10:00 МСК"),
        ("0 12 31 12 *", "каждый год 31 декабря в 12:00 МСК"),
        ("15 * * * *", "по cron «15 * * * *» (МСК)"),
    ],
)
def test_describe_says_the_rule_in_words(cron, words) -> None:
    assert describe(cron) == words


def test_next_run_handles_monthly_and_sunday_seven() -> None:
    friday = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)
    assert next_after("0 9 * * 1", friday).isoformat() == "2026-11-09T06:00:00+00:00"
    # Was limited to 8 days: monthly rules failed with bad_schedule.
    assert next_after("0 10 1 * *", friday).isoformat() == "2026-12-01T07:00:00+00:00"
    # 7 is Sunday in cron, as 0 is.
    assert next_after("0 10 * * 7", friday) == next_after("0 10 * * 0", friday)


def test_registered_as_a_server_tool(client) -> None:
    assert "schedule_create" in client_tool_names()
    assert executor_for("schedule_create") is schedule_tool.execute


def test_saves_a_heli_schedule_and_says_cron_and_words(session) -> None:
    user = _user(session)
    result = _create(session, user)
    assert result.error is None
    row = session.scalars(select(Schedule).where(Schedule.user_id == user.id)).one()
    assert row.expression == "0 9 * * 1"
    assert row.enabled
    assert "0 9 * * 1" in result.content
    assert "каждый понедельник в 9:00 МСК" in result.content
    assert "Europe/Moscow" in result.content
    assert "Битрикс24" in result.content  # «ничего не записано» во внешние системы


def test_accepts_the_monday_phrase(session) -> None:
    user = _user(session)
    result = _create(session, user, expression="каждый понедельник в 9:00")
    assert result.error is None
    assert session.scalars(select(Schedule.expression).where(Schedule.user_id == user.id)).one() == "0 9 * * 1"


@pytest.mark.parametrize("expression", ["*/15 * * * *", "0 9 * * 1-5", "каждые 15 минут", "0 0 31 2 *"])
def test_unsupported_rule_is_refused_without_saving(session, expression) -> None:
    user = _user(session)
    result = _create(session, user, expression=expression)
    assert result.error == "bad_schedule"
    assert "0 9 * * 1" in result.content  # the model gets an example to retry with
    assert _count(session, user) == 0


def test_same_schedule_twice_is_saved_once(session) -> None:
    user = _user(session)
    _create(session, user)
    again = _create(session, user)
    assert again.error is None
    assert "уже есть" in again.content
    assert _count(session, user) == 1


def test_per_user_cap(session, monkeypatch) -> None:
    monkeypatch.setattr(schedule_tool, "MAX_PER_USER", 2)
    user = _user(session)
    _create(session, user, request="раз")
    _create(session, user, request="два")
    third = _create(session, user, request="три")
    assert third.error == "too_many_schedules"
    assert _count(session, user) == 2


def test_a_scheduled_run_cannot_create_schedules(session) -> None:
    user = _user(session)
    task = start_task(session, user, "Кратко напомни про учебный ящик.", {"schedule_id": "00000000-0000-0000-0000-000000000001"})
    result = _create(session, user, task_id=task.id)
    assert result.error == "schedule_from_schedule"
    assert _count(session, user) == 0


def test_secrets_in_the_request_are_not_stored(session) -> None:
    user = _user(session)
    _create(session, user, request="Зайди с паролем: S3cr3t-Pa55 и токеном Bearer abc.DEF-123, напомни про ящик")
    stored = session.scalars(select(Schedule.request_text).where(Schedule.user_id == user.id)).one()
    assert "S3cr3t-Pa55" not in stored
    assert "abc.DEF-123" not in stored
    assert "напомни про ящик" in stored


def test_prompt_routes_recurring_tasks_to_the_heli_schedule() -> None:
    with_tool = system_prompt([schedule_tool.TOOL])
    assert "schedule_create" in with_tool
    assert "не создавай" not in with_tool
    assert "«в Битрикс24»" in with_tool
    assert "0 9 * * 1 — каждый понедельник в 9:00 МСК" in with_tool
    # Without the tool: say honestly it cannot be saved, and do not make a CRM task instead.
    without = system_prompt([])
    assert "schedule_create" not in without
    assert "расписание Heli сохранить пока нельзя" in without
    assert "не создавай вместо него задачу" in without


def test_eval_world_uses_the_prod_tool() -> None:
    world = World.build()
    result = world.call("schedule_create", {"label": "x", "expression": "0 9 * * 1", "request": "напомни"})
    assert world.state["heli"]["schedules"][0]["expression"] == "0 9 * * 1"
    assert "09.11.2026 09:00 МСК" in result["content"]  # world today is Monday 02.11, noon
    bad = world.call("schedule_create", {"label": "x", "expression": "0 9 * * 1-5", "request": "напомни"})
    assert bad["error"] == "bad_schedule"
    assert json.dumps(schedule_tool.TOOL, ensure_ascii=False)


def test_the_run_offers_the_tool_and_saves_from_the_chat(client, session, monkeypatch) -> None:
    from tests.test_agent_loop import _BrowserScript, _run_with_client

    script = _BrowserScript(
        tool="schedule_create",
        arguments=json.dumps({"label": "Сводка", "expression": "0 9 * * 1", "request": "Напомни про учебный ящик."}),
    )
    events = _run_with_client(
        client, session, monkeypatch, "Поставь повторяющуюся задачу: каждый понедельник в 9:00 напомнить про ящик",
        script, [], {"content": "не должен вызываться"},
    )
    offered = {tool["function"]["name"] for tool in script.requests[1]["tools"]}
    assert "schedule_create" in offered
    assert schedule_tool.SCHEDULE_HINT in script.requests[1]["messages"][0]["content"]
    assert not any(e["type"] == "client_action" for e in events)  # runs on the server, not in the app
    tool_reply = next(m for m in script.requests[2]["messages"] if m.get("role") == "tool")
    assert "0 9 * * 1" in tool_reply["content"]
    session.expire_all()
    assert session.scalars(select(Schedule.expression)).all() == ["0 9 * * 1"]
