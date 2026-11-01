"""Background runs: one thread per task, an event log the app can stream and resume from.

State lives in this process (bot-api runs one worker). A restart ends the runs in flight.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.agent.control import Control
from app.agent.task_run import execute_task
from app.llm.client import HeliClient, HeliError
from app.models import Task, User
from app.quota import QuotaError
from app.trajectories import TrajectoryError, auto_finish

log = logging.getLogger(__name__)

KEEP_FINISHED_S = 15 * 60
HEARTBEAT_S = 15.0
TERMINAL = {"done", "error"}


@dataclass
class AgentRun:
    task_id: uuid.UUID
    user_id: uuid.UUID
    control: Control = field(default_factory=Control)
    events: list[dict] = field(default_factory=list)
    finished_at: float | None = None
    thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        self._cond = threading.Condition()

    def publish(self, event: dict) -> None:
        with self._cond:
            event = {"seq": len(self.events) + 1, **event}
            if event["type"] in TERMINAL:
                self.finished_at = time.monotonic()
            self.events.append(event)
            self._cond.notify_all()

    def forget_secret_args(self, call_id: str) -> None:
        """client_action carries real args (maybe a password); once answered, replays get the safe copy."""
        with self._cond:
            for event in self.events:
                if event.get("type") == "client_action" and event.get("call_id") == call_id:
                    event["args"] = event.get("safe_args", {})
                    event["answered"] = True

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    def stream(self, after: int = 0, heartbeat: float = HEARTBEAT_S) -> Iterator[dict | None]:
        """Yield events after `after`; None means 'send a heartbeat'. Ends after done/error."""
        index = max(0, after)
        while True:
            with self._cond:
                if index >= len(self.events):
                    if self.finished:
                        return
                    self._cond.wait(heartbeat)
                    if index >= len(self.events):
                        yield None
                        continue
                batch = self.events[index:]
                index = len(self.events)
            for event in batch:
                yield _public_event(event)
                if event["type"] in TERMINAL:
                    return


def _public_event(event: dict) -> dict:
    public = {k: v for k, v in event.items() if k != "safe_args"}
    if event.get("answered"):
        public.pop("answered", None)
    return public


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, AgentRun] = {}
        self._lock = threading.Lock()

    def get(self, task_id: uuid.UUID, user_id: uuid.UUID) -> AgentRun | None:
        with self._lock:
            run = self._runs.get(task_id)
        if run is None or run.user_id != user_id:
            return None
        return run

    def start(
        self,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        session_factory: Callable[[], Session],
        client: HeliClient,
        client_tools: list[str],
        estimate_rub: Decimal | None,
        autopause: bool,
        control: Control | None = None,
    ) -> AgentRun:
        self._sweep()
        with self._lock:
            existing = self._runs.get(task_id)
            if existing is not None and not existing.finished:
                return existing
            run = AgentRun(task_id=task_id, user_id=user_id, control=control or Control())
            self._runs[task_id] = run

        def job() -> None:
            session = session_factory()
            try:
                user = session.get(User, user_id)
                task = session.get(Task, task_id)
                if user is None or task is None:
                    run.publish({"type": "error", "code": "task_not_found"})
                    return
                run.publish({"type": "started", "task_id": str(task_id)})
                turn = execute_task(
                    session,
                    user,
                    task,
                    client,
                    client_tools=client_tools,
                    control=run.control,
                    publish=run.publish,
                    estimate_rub=estimate_rub,
                    autopause=autopause,
                )
                session.commit()
                _close_task(session, task_id, "aborted" if turn.stopped else "success", turn.text)
                run.publish(
                    {
                        "type": "done",
                        "reply": turn.text,
                        "alias": turn.alias,
                        "task_type": turn.task_type,
                        "complexity": turn.complexity,
                        "cost_rub": format(turn.cost_rub, "f"),
                        "stopped": turn.stopped,
                        "steps": turn.steps,
                    }
                )
            except (QuotaError, TrajectoryError, HeliError) as exc:
                session.rollback()
                _close_task(session, task_id, "fail", None)
                run.publish({"type": "error", "code": exc.code})
            except Exception:  # noqa: BLE001
                log.exception("agent run failed task=%s", task_id)
                session.rollback()
                _close_task(session, task_id, "fail", None)
                run.publish({"type": "error", "code": "run_failed"})
            finally:
                session.close()

        run.thread = threading.Thread(target=job, name=f"heli-run-{task_id}", daemon=True)
        run.thread.start()
        return run

    def _sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, r in self._runs.items() if r.finished_at and now - r.finished_at > KEEP_FINISHED_S]:
                del self._runs[key]


registry = RunRegistry()


def _close_task(session: Session, task_id: uuid.UUID, outcome: str, final_answer: str | None) -> None:
    """Give the task an outcome right after the run, so unrated tasks also become trajectories."""
    try:
        auto_finish(session, task_id, outcome, final_answer)
        session.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not close task=%s", task_id)
        session.rollback()
