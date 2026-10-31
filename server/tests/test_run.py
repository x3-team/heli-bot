from datetime import datetime, timezone
from decimal import Decimal

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
