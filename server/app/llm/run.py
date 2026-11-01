"""One chat turn: classify, route, answer, and store the steps."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.llm.client import HeliClient
from app.llm.router import Router, classification_messages, parse_classification
from app.llm.tools import TOOLS, call_tool, parse_args
from app.memory import relevant, with_memory
from app.models import Task, User
from app.quota import metered_complete, remaining_ratio, snapshot
from app.trajectories import add_step

_ANSWER = (
    "Ты Heli, ассистент для рабочих задач. Отвечай по-русски и по делу. "
    "У тебя есть доступ в интернет: инструмент web_search ищет в сети, open_url открывает и читает страницу. "
    "Когда нужны свежие данные, ссылки, цены, контакты или проверка фактов, сначала найди и открой источник, "
    "потом отвечай. Давай прямые ссылки на сайты и указывай, откуда взял данные. "
    "Не говори, что у тебя нет доступа к интернету. Если поиск не дал результата, открой известный "
    "официальный сайт через open_url. Не называй другие модели. Если действие опасное, опиши его и остановись. "
    "Сегодня {today}."
)
_ANSWER_MAX_TOKENS = 1500
MAX_TOOL_ROUNDS = 6
_TOOL_RESULT_LIMIT = 9000
_MSK = ZoneInfo("Europe/Moscow")


@dataclass
class Turn:
    alias: str
    text: str
    task_type: str
    complexity: int
    input_tokens: int
    output_tokens: int
    cost_rub: Decimal


def run_turn(session: Session, user: User, task: Task, client: HeliClient) -> Turn:
    prompt = with_memory(task.request_text, relevant(session, user, task.request_text))
    classified = metered_complete(
        session,
        user,
        task.id,
        client,
        "heli-fast",
        classification_messages(prompt),
        max_tokens=80,
    )
    task_type, complexity = parse_classification(classified.text)
    add_step(
        session,
        user,
        task.id,
        thought="классификация",
        model_alias=classified.alias,
        reply=classified.text,
        input_tokens=classified.input_tokens,
        output_tokens=classified.output_tokens,
        cost_rub=classified.cost_rub,
        attempt=1,
    )
    session.commit()
    ratio = remaining_ratio(snapshot(session, user))
    route = Router(client).route(phase="execute", complexity=complexity, quota_remaining=ratio)
    messages: list[dict] = [
        {"role": "system", "content": _ANSWER.format(today=datetime.now(_MSK).strftime("%d.%m.%Y"))},
        {"role": "user", "content": prompt},
    ]
    input_tokens = classified.input_tokens
    output_tokens = classified.output_tokens
    total_cost = classified.cost_rub
    answer = None
    for round_index in range(MAX_TOOL_ROUNDS + 1):
        last = round_index == MAX_TOOL_ROUNDS
        answer = metered_complete(
            session,
            user,
            task.id,
            client,
            route.alias,
            messages,
            max_tokens=_ANSWER_MAX_TOKENS,
            tools=None if last else TOOLS,
        )
        input_tokens += answer.input_tokens
        output_tokens += answer.output_tokens
        total_cost += answer.cost_rub
        if not answer.tool_calls:
            break
        messages.append(answer.assistant_message())
        add_step(
            session,
            user,
            task.id,
            thought="план" if answer.text else "инструменты",
            plan=task_type,
            model_alias=answer.alias,
            reply=answer.text or None,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            cost_rub=answer.cost_rub,
            attempt=1,
        )
        for call in answer.tool_calls:
            args = parse_args(call.arguments)
            result, error = call_tool(call.name, args)
            result = result[:_TOOL_RESULT_LIMIT]
            add_step(
                session,
                user,
                task.id,
                thought="инструмент",
                tool_name=call.name,
                tool_args=args,
                result_text=result,
                error=error,
                attempt=1,
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        session.commit()
    assert answer is not None
    add_step(
        session,
        user,
        task.id,
        thought="ответ",
        plan=task_type,
        model_alias=answer.alias,
        reply=answer.text,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
        cost_rub=answer.cost_rub,
        attempt=1,
    )
    return Turn(
        alias=answer.alias,
        text=answer.text,
        task_type=task_type,
        complexity=complexity,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_rub=total_cost,
    )
