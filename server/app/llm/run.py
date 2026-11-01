"""One chat turn without a live client: server tools only, auto-pause ends the run.

The streaming endpoint uses app.agent.runs; both share app.agent.task_run.execute_task.
"""

from sqlalchemy.orm import Session

from app.agent.task_run import Turn, execute_task
from app.llm.client import HeliClient
from app.models import Task, User

MAX_TOOL_ROUNDS = 6

__all__ = ["Turn", "run_turn", "MAX_TOOL_ROUNDS"]


def run_turn(session: Session, user: User, task: Task, client: HeliClient) -> Turn:
    return execute_task(session, user, task, client, max_steps=MAX_TOOL_ROUNDS + 1, autopause=False)
