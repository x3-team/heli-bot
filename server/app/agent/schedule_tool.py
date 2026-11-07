"""Agent tool for Heli's own schedules: «поставь повторяющуюся задачу / напоминание» from the chat.

schedule_create(label, expression, request) saves a row in app.schedules (the same table as the
«Расписания» screen and POST /v1/schedules) and returns how it was saved: cron, the rule in words and the
next run, all in Moscow time. A recurring task for the assistant itself is a Heli schedule, not a task in
Битрикс24 or another CRM; the CRM is used only when the user names it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent.control import ClientResult
from app.masking import redact_secrets_text

log = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")
TOOL_NAME = "schedule_create"
# A user who needs more is better served by the «Расписания» screen than by a model looping on this tool.
MAX_PER_USER = 20

TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Поставить повторяющуюся задачу или напоминание в расписание Heli (раздел «Расписания»): по "
            "расписанию Heli выполнит request, результат виден в «Расписаниях». Это для просьб вроде «поставь "
            "повторяющуюся задачу», «напоминай мне каждый…», «каждый понедельник в 9:00 делай сводку», когда "
            "пользователь не назвал внешнюю систему (Битрикс24, amoCRM, CRM, календарь). "
            "expression — cron из 5 полей по московскому времени: минута час число месяц день_недели "
            "(1 — понедельник, 0 — воскресенье), только числа и *, без диапазонов и шагов. "
            "Пример: «0 9 * * 1» — каждый понедельник в 9:00 МСК. Ответ — сохранённая запись: cron, правило "
            "словами и ближайший запуск; перечитывать её не нужно."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Короткое название, например «Сводка по учебному ящику»"},
                "expression": {"type": "string", "description": "cron из 5 полей по МСК, например «0 9 * * 1»"},
                "request": {
                    "type": "string",
                    "description": "Что Heli сделать при каждом запуске, как задача от пользователя",
                },
            },
            "required": ["label", "expression", "request"],
        },
    },
}

SCHEDULE_HINT = (
    " Повторяющиеся задачи и напоминания себе («поставь повторяющуюся задачу», «напоминай мне каждый…», "
    "«каждый понедельник в 9:00 сделай сводку») — это расписание Heli: ставь их через schedule_create, а не "
    "задачей в Битрикс24, amoCRM или другой внешней системе. Внешнюю систему используй, только если "
    "пользователь прямо её назвал («в Битрикс24», «в CRM», «на портале»). В ответе скажи, как записалось: "
    "cron и словами, например «0 9 * * 1 — каждый понедельник в 9:00 МСК», и ближайший запуск."
)
NO_SCHEDULE_HINT = (
    " Сохранять расписания Heli в этом приложении пока нельзя. На просьбу поставить повторяющуюся задачу или "
    "напоминание себе честно скажи, что расписание Heli сохранить пока нельзя, и не создавай вместо него задачу "
    "в Битрикс24, amoCRM или другой внешней системе, если пользователь прямо её не назвал."
)


def saved_text(label: str, expression: str, next_run: datetime, request: str, *, existed: bool = False) -> str:
    """What the model reads after a save. The world of the evals uses it too."""
    from app.schedules import describe

    head = "Такое расписание Heli уже есть" if existed else "Сохранено в расписание Heli"
    when = next_run.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    return (
        f"{head} (раздел «Расписания»): «{label}».\n"
        f"cron: {expression} (часовой пояс Europe/Moscow) — {describe(expression)}.\n"
        f"Ближайший запуск: {when} МСК.\n"
        f"При запуске Heli выполнит: {request}\n"
        "В Битрикс24, CRM и другие внешние системы ничего не записано."
    )


def execute(session, user, task_id, name: str, args: dict) -> ClientResult:
    """app.agent.executors contract."""
    from app.models import Schedule, Task
    from app.schedules import ScheduleError, create_schedule, next_after, parse_expression

    if name != TOOL_NAME:
        return ClientResult("Ошибка: unknown_tool", "unknown_tool")
    task = session.get(Task, task_id) if isinstance(task_id, uuid.UUID) else None
    if task is not None and isinstance(task.context, dict) and task.context.get("schedule_id"):
        # A scheduled run must not multiply itself.
        return ClientResult(
            "Ошибка: это запуск по расписанию, новое расписание из него не ставится.", "schedule_from_schedule"
        )
    request = redact_secrets_text(str(args.get("request") or "").strip())[:4000]
    label = redact_secrets_text(str(args.get("label") or "").strip()) or request[:60]
    if not request:
        return ClientResult("Ошибка: укажи в request, что Heli делать при каждом запуске.", "bad_request")
    try:
        cron = parse_expression(str(args.get("expression") or ""))
        next_after(cron, datetime.now(MSK))  # «31 февраля» never comes: refuse before saving
    except ScheduleError as exc:
        return ClientResult(
            "Ошибка: не понял расписание. Нужен cron из 5 полей по МСК, только числа и *: "
            "«0 9 * * 1» — каждый понедельник в 9:00.",
            exc.code,
        )
    same = session.scalars(
        select(Schedule).where(
            Schedule.user_id == user.id, Schedule.expression == cron, Schedule.request_text == request
        )
    ).first()
    if same is not None:
        return ClientResult(saved_text(same.label, same.expression, same.next_run, same.request_text, existed=True))
    count = session.scalar(select(func.count()).select_from(Schedule).where(Schedule.user_id == user.id)) or 0
    if count >= MAX_PER_USER:
        return ClientResult(
            f"Ошибка: у пользователя уже {count} расписаний Heli, больше {MAX_PER_USER} не ставится. "
            "Предложи удалить лишние в разделе «Расписания».",
            "too_many_schedules",
        )
    try:
        row = create_schedule(session, user, label, cron, request)
    except ScheduleError as exc:
        if exc.code != "schedule_limit":
            raise
        return ClientResult(
            "Ошибка: на тарифе пользователя расписаний больше не поставить. "
            "Предложи удалить лишнее в разделе «Расписания» или перейти на тариф выше.",
            "too_many_schedules",
        )
    log.info("schedule saved from chat id=%s", row.id)
    return ClientResult(saved_text(row.label, row.expression, row.next_run, row.request_text))


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(TOOL)
    register_executor({TOOL_NAME}, execute)
