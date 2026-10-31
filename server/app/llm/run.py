"""One chat turn: classify, route, answer, and store the steps."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.llm.client import HeliClient
from app.llm.router import Router, classification_messages, parse_classification
from app.models import Task, User
from app.quota import metered_complete, remaining_ratio, snapshot
from app.trajectories import add_step

_ANSWER = (
    "Ты ассистент Heli для рабочей задачи. Отвечай по-русски и по делу. "
    "Не называй другие модели. Если действие опасное, опиши его и остановись."
)
_ANSWER_MAX_TOKENS = 256


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
    classified = metered_complete(
        session,
        user,
        task.id,
        client,
        "heli-fast",
        classification_messages(task.request_text),
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
    answer = metered_complete(
        session,
        user,
        task.id,
        client,
        route.alias,
        [
            {"role": "system", "content": _ANSWER},
            {"role": "user", "content": task.request_text},
        ],
        max_tokens=_ANSWER_MAX_TOKENS,
    )
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
        input_tokens=classified.input_tokens + answer.input_tokens,
        output_tokens=classified.output_tokens + answer.output_tokens,
        cost_rub=classified.cost_rub + answer.cost_rub,
    )
