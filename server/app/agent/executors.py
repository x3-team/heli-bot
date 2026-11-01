"""Server-side executors for browser tools (the cloud computer).

The cloud computer registers itself here. A registered tool is offered to the model even when
the app did not declare it, and runs on the server instead of being sent to the app.

    register_executor({"browser_open", ...}, run)   # run(session, user, task_id, name, args) -> ClientResult
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from app.agent.control import ClientResult
from app.models import User

Executor = Callable[[Session, User, object, str, dict], ClientResult]

_EXECUTORS: dict[str, Executor] = {}


def register_executor(tools: Iterable[str], run: Executor) -> None:
    for name in tools:
        _EXECUTORS[name] = run


def unregister_executor(tools: Iterable[str]) -> None:
    for name in tools:
        _EXECUTORS.pop(name, None)


def executor_for(name: str) -> Executor | None:
    return _EXECUTORS.get(name)


def server_executed() -> set[str]:
    return set(_EXECUTORS)
