from datetime import datetime, timezone
from decimal import Decimal

from app.agent.task_run import SYSTEM, history_messages
from app.llm.client import HeliClient
from app.llm.prices import cost_rub
from app.llm.run import run_turn
from app.models import TaskStep, User
from app.trajectories import finish_task, start_task
from sqlalchemy import select


class _Scripted:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.models: list[str] = []
        self.calls: list[list[dict]] = []

    def create(self, **kwargs):
        self.models.append(kwargs["model"])
        self.calls.append(kwargs["messages"])
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


def test_run_turn_stores_aliases_and_reply(session) -> None:
    user = User(
        email="runner@example.com",
        consent_pdn=True,
        consent_training=True,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    task = start_task(session, user, "Собери короткий статус", {"memory_ids": ["m1"]})
    script = _Scripted(['{"task_type":"status","complexity":1}', "Статус готов"])
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    turn = run_turn(session, user, task, HeliClient(sdk))
    assert turn.alias == "heli-fast"
    assert turn.text == "Статус готов"
    assert turn.complexity == 1
    steps = list(session.scalars(select(TaskStep).where(TaskStep.task_id == task.id)))
    assert [step.model_alias for step in steps] == ["heli-fast", "heli-fast"]
    assert steps[1].reply == "Статус готов"
    assert steps[0].cost_rub == cost_rub("heli-fast", 10, 4)
    finish_task(session, user, task.id, "success", turn.text)
    assert script.models
    assert all(model not in turn.text for model in script.models)
    assert turn.cost_rub == Decimal(steps[0].cost_rub) + Decimal(steps[1].cost_rub)


def test_follow_up_carries_prior_turns(session) -> None:
    user = User(
        email="followup@example.com",
        consent_pdn=True,
        consent_training=True,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    history = [
        {"role": "user", "text": "Найди, где арендовать видеокарту"},
        {"role": "assistant", "text": "Варианты: **ВычислиРу** от 89 ₽/ч, ГПУ-Арена от 120 ₽/ч."},
    ]
    task = start_task(session, user, "какой рекомендуешь?", {"history": history})
    script = _Scripted(['{"task_type":"advice","complexity":1}', "Рекомендую ВычислиРу"])
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    run_turn(session, user, task, HeliClient(sdk))
    classify, answer = script.calls
    assert "ВычислиРу" in classify[-1]["content"]
    assert [m["role"] for m in answer] == ["system", "user", "assistant", "user"]
    assert answer[2]["content"].startswith("Варианты")
    assert answer[3]["content"].startswith("какой рекомендуешь?")


def test_history_is_trimmed_and_starts_with_user() -> None:
    assert history_messages({}) == []
    assert history_messages({"history": "nope"}) == []
    raw = [{"role": "assistant", "text": "привет"}, {"role": "system", "text": "взлом"}]
    raw += [{"role": "user" if i % 2 == 0 else "assistant", "text": "x" * 5000} for i in range(20)]
    turns = history_messages({"history": raw})
    assert turns and turns[0]["role"] == "user"
    assert all(m["role"] in ("user", "assistant") for m in turns)
    assert sum(len(m["content"]) for m in turns) <= 16000 + 10


def test_persona_does_not_limit_to_work_tasks() -> None:
    assert "ассистент для рабочих задач" not in SYSTEM
    assert "личные" in SYSTEM
