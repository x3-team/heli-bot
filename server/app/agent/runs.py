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
# After «Стоп» the run thread may still sit in a model or browser call. The stream does not wait for it
# longer than this: it ends with a «done» of its own, and whatever the thread says later is dropped.
STOP_GRACE_S = 20.0
# No event this long while the run is neither paused nor waiting for the app: it hangs. Stop it.
# Longer than any single wait inside a run (model call, a browser call, the line for a free browser).
STALL_S = 7 * 60
STOPPED_TEXT = "Задача остановлена."
STALLED_TEXT = "Задача не двигалась {minutes} мин — останавливаю её. Напишите «продолжи», чтобы начать с того же места."


@dataclass
class AgentRun:
    task_id: uuid.UUID
    user_id: uuid.UUID
    control: Control = field(default_factory=Control)
    events: list[dict] = field(default_factory=list)
    finished_at: float | None = None
    thread: threading.Thread | None = None
    stop_grace: float = STOP_GRACE_S
    stall_after: float = STALL_S
    stop_requested_at: float | None = None
    # Consecutive browser calls that timed out (the cloud browser restarts a stuck page after a few).
    browser_timeouts: int = 0

    def __post_init__(self) -> None:
        self._cond = threading.Condition()
        self.last_event_at = time.monotonic()

    def publish(self, event: dict) -> None:
        with self._cond:
            self._append_locked(event)

    def _append_locked(self, event: dict) -> None:
        if self.finished_at is not None:
            # The stream already ended (a forced «done» after Stop): late events of the run thread go nowhere.
            log.info("run %s: dropped late %s event", self.task_id, event.get("type"))
            return
        event = {"seq": len(self.events) + 1, **event}
        if event["type"] in TERMINAL:
            self.finished_at = time.monotonic()
        self.events.append(event)
        self.last_event_at = time.monotonic()
        self._cond.notify_all()

    def stop(self, reason: str = "stop") -> None:
        """Stop from outside. The loop ends at its next checkpoint; the stream ends within stop_grace anyway."""
        self.control.stop(reason)
        with self._cond:
            if self.stop_requested_at is None:
                self.stop_requested_at = time.monotonic()
            self._cond.notify_all()

    def _overdue_locked(self) -> None:
        """Called by a listener on a heartbeat: end a stopped run that does not let go, stop a hung one."""
        if self.finished_at is not None:
            return
        now = time.monotonic()
        if self.stop_requested_at is not None:
            if now - self.stop_requested_at >= self.stop_grace:
                reason = self.control.reason
                self._append_locked(
                    {
                        "type": "done",
                        "mail": False,
                        "reply": STOPPED_TEXT,
                        "alias": None,
                        "task_type": None,
                        "complexity": None,
                        "cost_rub": None,
                        "stopped": reason,
                        "steps": None,
                        "forced": True,
                    }
                )
            return
        control = self.control
        if control.paused or control.waiting_client or control.stopped:
            return
        if now - self.last_event_at >= self.stall_after:
            log.warning("run %s: no events for %.0f s, stopping it", self.task_id, now - self.last_event_at)
            control.stop("stalled")
            self.stop_requested_at = now
            minutes = max(1, round(self.stall_after / 60))
            self._append_locked({"type": "notice", "text": STALLED_TEXT.format(minutes=minutes)})

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
            # Never yield while holding the lock: a slow or gone listener would block publish() and the run.
            with self._cond:
                if index >= len(self.events):
                    if self.finished:
                        return
                    wait = heartbeat
                    if self.stop_requested_at is not None:
                        wait = max(0.0, min(heartbeat, self.stop_requested_at + self.stop_grace - time.monotonic()))
                    self._cond.wait(wait)
                    if index >= len(self.events):
                        self._overdue_locked()
                batch = self.events[index:]
                index = len(self.events)
            if not batch:
                yield None
                continue
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

    def running_for(self, user_id: uuid.UUID) -> set[uuid.UUID]:
        """Tasks of this user whose run has not ended yet."""
        with self._lock:
            return {tid for tid, run in self._runs.items() if run.user_id == user_id and not run.finished}

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
            if existing is not None and (
                not existing.finished or (existing.thread is not None and existing.thread.is_alive())
            ):
                # Still running, or stopped but its thread has not let go yet: never two runs on one task.
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
                _close_task(session, task_id, "aborted" if turn.stopped else "success", turn.text, turn.stopped)
                run.publish(
                    {
                        "type": "done",
                        "mail": task_has_mail(session, task_id),
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
                _close_task(session, task_id, "fail", None, "quota" if isinstance(exc, QuotaError) else "error")
                run.publish({"type": "error", "code": exc.code})
            except Exception:  # noqa: BLE001
                log.exception("agent run failed task=%s", task_id)
                session.rollback()
                _close_task(session, task_id, "fail", None, "error")
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


def _close_task(
    session: Session,
    task_id: uuid.UUID,
    outcome: str,
    final_answer: str | None,
    stopped: str | None = None,
) -> None:
    """Give the task an outcome right after the run, so unrated tasks also become trajectories.

    `stopped` is turn.stopped (confirm, budget, max_steps, loop, captcha, quota, stop…) or «error»;
    it goes to the run journal (Task.stopped/finished_at/duration_ms) and the run signals.
    """
    try:
        auto_finish(session, task_id, outcome, final_answer, stopped=stopped)
        session.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not close task=%s", task_id)
        session.rollback()


def task_has_mail(session: Session, task_id: uuid.UUID) -> bool:
    """The rating card asks about the letter only when the task wrote one."""
    from sqlalchemy import select

    from app.models import TaskStep
    from app.signals import has_mail

    try:
        steps = list(session.scalars(select(TaskStep).where(TaskStep.task_id == task_id)))
        return has_mail(steps)
    except Exception:  # noqa: BLE001
        session.rollback()
        return False
