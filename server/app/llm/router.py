"""Rule router. It never returns an upstream model name."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from app.llm.client import Completion, HeliClient

_CLASSIFY = (
    "Определи тип задачи и сложность от 1 до 3. "
    "Ответь только JSON: {\"task_type\":\"...\",\"complexity\":1}. "
    "1 — простой шаг, 2 — обычная задача, 3 — план и восстановление после ошибок."
)


@dataclass
class Route:
    alias: str
    phase: str


def choose_alias(
    *,
    phase: str,
    complexity: int,
    needs_vision: bool,
    consecutive_errors: int,
    quota_remaining: Decimal,
) -> str:
    smart_blocked = quota_remaining < Decimal("0.20")
    if consecutive_errors >= 2 and not smart_blocked:
        return "heli-smart"
    if phase == "plan" and complexity >= 2 and not smart_blocked:
        return "heli-smart-alt"
    if phase == "execute" and needs_vision:
        return "heli-vision"
    return "heli-fast"


class Router:
    def __init__(self, client: HeliClient) -> None:
        self._client = client

    def classify(self, task: str) -> tuple[str, int, Completion]:
        completion = self._client.complete(
            "heli-fast",
            [
                {"role": "system", "content": _CLASSIFY},
                {"role": "user", "content": task},
            ],
            max_tokens=80,
        )
        return (*_parse(completion.text), completion)

    def route(
        self,
        *,
        phase: str,
        complexity: int,
        needs_vision: bool = False,
        consecutive_errors: int = 0,
        quota_remaining: Decimal = Decimal("1"),
    ) -> Route:
        alias = choose_alias(
            phase=phase,
            complexity=complexity,
            needs_vision=needs_vision,
            consecutive_errors=consecutive_errors,
            quota_remaining=quota_remaining,
        )
        return Route(alias=alias, phase=phase)


def _parse(text: str) -> tuple[str, int]:
    try:
        data = json.loads(text)
        complexity = int(data.get("complexity", 2))
        task_type = str(data.get("task_type") or "task")
    except (json.JSONDecodeError, TypeError, ValueError):
        return "task", 2
    if complexity < 1:
        complexity = 1
    if complexity > 3:
        complexity = 3
    return task_type, complexity
