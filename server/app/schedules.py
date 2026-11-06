"""Cron schedules. Each run spends quota and leaves the result on the task."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.browser import BrowserError, pool_start
from app.llm.client import HeliClient, HeliError
from app.llm.run import run_turn
from app.masking import redact_secrets_text
from app.models import Schedule, User
from app.notify import notify
from app.quota import QuotaError
from app.trajectories import TrajectoryError, finish_task, start_task

log = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

_PHRASES = {
    "каждый понедельник в 9:00": "0 9 * * 1",
    "каждый понедельник в 09:00": "0 9 * * 1",
}


class ScheduleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_expression(text: str) -> str:
    cleaned = " ".join(text.strip().lower().split())
    if cleaned in _PHRASES:
        return _PHRASES[cleaned]
    parts = cleaned.split(" ")
    if len(parts) != 5 or not all(_field_ok(part, limit) for part, limit in zip(parts, (59, 23, 31, 12, 7), strict=True)):
        raise ScheduleError("bad_schedule")
    return cleaned


# cron day of week (0 and 7 are Sunday) in the accusative: «каждый понедельник», «каждую среду».
_EVERY_WEEKDAY = (
    "каждое воскресенье",
    "каждый понедельник",
    "каждый вторник",
    "каждую среду",
    "каждый четверг",
    "каждую пятницу",
    "каждую субботу",
    "каждое воскресенье",
)
_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def describe(expression: str) -> str:
    """A cron line in words, Moscow time: "0 9 * * 1" -> "каждый понедельник в 9:00 МСК"."""
    minute, hour, day, month, weekday = expression.split(" ")
    if not (minute.isdigit() and hour.isdigit()):
        return f"по cron «{expression}» (МСК)"
    at = f"в {int(hour)}:{int(minute):02d} МСК"
    if day == "*" and month == "*":
        if weekday == "*":
            return f"каждый день {at}"
        return f"{_EVERY_WEEKDAY[int(weekday)]} {at}"
    if weekday == "*" and day.isdigit():
        if month == "*":
            return f"каждый месяц {int(day)}-го числа {at}"
        return f"каждый год {int(day)} {_MONTHS_GEN[int(month) - 1]} {at}"
    return f"по cron «{expression}» (МСК)"


def create_schedule(session: Session, user: User, label: str, expression: str, request: str, now: datetime | None = None) -> Schedule:
    cron = parse_expression(expression)
    moment = _aware(now)
    row = Schedule(
        user_id=user.id,
        label=label.strip()[:200],
        expression=cron,
        request_text=redact_secrets_text(request),
        enabled=True,
        next_run=next_after(cron, moment),
        last_run=None,
        last_result=None,
        last_error=None,
        created_at=moment,
    )
    session.add(row)
    session.flush()
    return row


def list_schedules(session: Session, user: User) -> list[dict]:
    rows = session.scalars(select(Schedule).where(Schedule.user_id == user.id).order_by(Schedule.created_at))
    return [_public(row) for row in rows]


def delete_schedule(session: Session, user: User, schedule_id: uuid.UUID) -> None:
    row = session.get(Schedule, schedule_id)
    if row is None or row.user_id != user.id:
        raise ScheduleError("not_found")
    session.delete(row)
    session.flush()


def run_due(
    session: Session,
    client: HeliClient,
    driver,
    *,
    user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> int:
    moment = _aware(now)
    query = select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run <= moment)
    if user_id is not None:
        query = query.where(Schedule.user_id == user_id)
    ran = 0
    for row in list(session.scalars(query)):
        _run_one(session, row, client, driver, moment)
        ran += 1
    return ran


def next_after(expression: str, now: datetime) -> datetime:
    """The first matching minute after `now`, Moscow time. Looks day by day, so monthly and yearly rules work too."""
    start = _aware(now).astimezone(MSK).replace(second=0, microsecond=0) + timedelta(minutes=1)
    _, _, day, month, weekday = expression.split(" ")
    # Up to 5 years: «29 февраля» comes round once in 4.
    for offset in range(366 * 5):
        date = (start + timedelta(days=offset)).date()
        if not (_hit(day, date.day) and _hit(month, date.month) and _hit_weekday(weekday, date.weekday())):
            continue
        local = start if offset == 0 else datetime(date.year, date.month, date.day, tzinfo=MSK)
        while local.date() == date:
            if _matches(expression, local):
                return local.astimezone(timezone.utc)
            local += timedelta(minutes=1)
    raise ScheduleError("bad_schedule")


def _run_one(session: Session, row: Schedule, client: HeliClient, driver, moment: datetime) -> None:
    user = session.get(User, row.user_id)
    if user is None:
        return
    try:
        pool_start(session, user.id, driver, prefer_local=False, now=moment)
    except BrowserError:
        log.info("schedule browser skipped")
    row.last_run = moment
    row.next_run = next_after(row.expression, moment)
    try:
        task = start_task(session, user, row.request_text, {"schedule_id": str(row.id)})
        turn = run_turn(session, user, task, client)
        finish_task(session, user, task.id, "success", turn.text)
    except QuotaError as exc:
        row.last_error = exc.code
        row.last_result = None
        session.flush()
        return
    except (HeliError, ScheduleError, TrajectoryError) as exc:
        row.last_error = exc.code
        session.flush()
        return
    row.last_error = None
    row.last_result = turn.text
    session.flush()
    notify(f"{row.label}: {turn.text}")


def _matches(expression: str, local: datetime) -> bool:
    minute, hour, day, month, weekday = expression.split(" ")
    return (
        _hit(minute, local.minute)
        and _hit(hour, local.hour)
        and _hit(day, local.day)
        and _hit(month, local.month)
        and _hit_weekday(weekday, local.weekday())
    )


def _hit_weekday(field: str, python_weekday: int) -> bool:
    """cron day of week: 0 and 7 are Sunday, 1 is Monday."""
    cron_dow = (python_weekday + 1) % 7
    return _hit("0" if field == "7" else field, cron_dow)


def _hit(field: str, value: int) -> bool:
    if field == "*":
        return True
    return int(field) == value


def _field_ok(field: str, limit: int) -> bool:
    if field == "*":
        return True
    if not field.isdigit():
        return False
    number = int(field)
    if limit == 7:
        return 0 <= number <= 7
    if limit == 31:
        return 1 <= number <= 31
    if limit == 12:
        return 1 <= number <= 12
    return 0 <= number <= limit


def _public(row: Schedule) -> dict:
    return {
        "id": str(row.id),
        "label": row.label,
        "expression": row.expression,
        "enabled": row.enabled,
        "next_run": row.next_run.isoformat(),
        "last_result": row.last_result,
        "last_error": row.last_error,
    }


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now
