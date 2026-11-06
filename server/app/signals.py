"""Implicit quality signals: what the user did after a task, and what the run itself shows.

The user is never asked about these and never sees their names. They go into the training document
(payload.signals) and the quality dashboard (/v1/admin/quality).

Follow-up signals (written on the PREVIOUS task when the next one starts, see on_task_start):
- continue        — «продолжи» after a stopped run (not a failure);
- retry           — almost the same request within 30 minutes;
- reformulate     — a short clarification of the same topic within 30 minutes;
- next_unrelated  — another topic while the previous card was not rated;
- abandon         — the card was closed with ✕ (dismiss), or not rated and nothing followed for 30 minutes
                    (checked lazily when the next task starts: there is no server timer in the MVP).

Run signals (recomputed at the end of a run, see on_run_end):
- confirm_approved / confirm_rejected / confirm_edited — TaskStep.confirmation;
- verify_ok / verify_fail — a read of the same service after a write passed / failed (no deep diff);
- loop / max_steps / captcha / budget — from Task.stopped.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TaskFeedback, TaskSignal, TaskStep, User

log = logging.getLogger(__name__)

FOLLOW_KINDS = {"continue", "retry", "reformulate", "next_unrelated", "abandon"}
CONFIRM_KINDS = {"confirm_approved", "confirm_rejected", "confirm_edited"}
VERIFY_KINDS = {"verify_ok", "verify_fail"}
STOP_KINDS = {"loop", "max_steps", "captcha", "budget"}
RUN_KINDS = CONFIRM_KINDS | VERIFY_KINDS | STOP_KINDS
KINDS = FOLLOW_KINDS | RUN_KINDS

FOLLOW_WINDOW = timedelta(minutes=30)
ABANDON_AFTER = timedelta(minutes=30)
LOOKBACK = timedelta(hours=24)
RETRY_SIMILARITY = 0.6
TOPIC_SIMILARITY = 0.2
SHORT_FOLLOWUP = 200

_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)
_STOP = {
    "и", "в", "на", "по", "с", "за", "для", "не", "что", "как", "это", "или", "из", "к", "от", "до", "у", "о",
    "а", "но", "ли", "мне", "нам", "мой", "наш", "пожалуйста", "please", "the", "a", "of", "to", "in", "and",
}
_MAIL_WORDS = re.compile(r"письм|почт|e-?mail|mail_send|отправ", re.I)
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stems(text: str) -> set[str]:
    return {w.lower()[:5] for w in _WORD.findall(text or "") if w.lower() not in _STOP and len(w) > 1}


def similarity(a: str, b: str) -> float:
    """Word-stem Jaccard: «найди аренду видеокарты» ~ «найди аренду видеокарт подешевле»."""
    left, right = _stems(a), _stems(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def is_continue(text: str) -> bool:
    from app.agent.task_run import _CONTINUE

    return bool(_CONTINUE.match(text or ""))


def classify_followup(prev_text: str, new_text: str, *, gap: timedelta, prev_rated: bool) -> str | None:
    """What the new request says about the previous task. None: nothing worth a signal."""
    if is_continue(new_text):
        return "continue"
    if gap <= FOLLOW_WINDOW:
        score = similarity(prev_text, new_text)
        if score >= RETRY_SIMILARITY:
            return "retry"
        if score >= TOPIC_SIMILARITY and len((new_text or "").strip()) <= SHORT_FOLLOWUP:
            return "reformulate"
        if not prev_rated:
            return "next_unrelated"
    return None


def record(
    session: Session,
    user_id: uuid.UUID,
    task_id: uuid.UUID | None,
    kind: str,
    payload: dict | None = None,
) -> TaskSignal:
    if kind not in KINDS:
        raise ValueError(kind)
    row = TaskSignal(task_id=task_id, user_id=user_id, kind=kind, payload=payload or {}, created_at=_now())
    session.add(row)
    session.flush()
    return row


def for_task(session: Session, task_id: uuid.UUID) -> list[TaskSignal]:
    return list(
        session.scalars(select(TaskSignal).where(TaskSignal.task_id == task_id).order_by(TaskSignal.created_at))
    )


def _has(session: Session, task_id: uuid.UUID, kind: str) -> bool:
    return session.scalar(
        select(TaskSignal.id).where(TaskSignal.task_id == task_id, TaskSignal.kind == kind).limit(1)
    ) is not None


def _rated(session: Session, task_id: uuid.UUID) -> bool:
    row = session.scalar(select(TaskFeedback).where(TaskFeedback.task_id == task_id))
    return row is not None and (row.rating is not None or row.acceptance is not None or bool(row.mail_disposition))


def checkpoint_parent(session: Session, user: User, task: Task) -> Task | None:
    """The stopped task «продолжи» picks up (same lookup as app.agent.task_run.continuation)."""
    from app.agent.task_run import CHECKPOINT_TTL

    since = _now() - CHECKPOINT_TTL
    rows = session.scalars(
        select(Task)
        .where(Task.user_id == user.id, Task.id != task.id, Task.created_at >= since)
        .order_by(Task.created_at.desc())
        .limit(5)
    ).all()
    for row in rows:
        point = (row.context or {}).get("checkpoint") if isinstance(row.context, dict) else None
        if isinstance(point, dict) and point.get("request"):
            return row
    return None


def on_task_start(session: Session, user: User, task: Task) -> list[str]:
    """Signals about the previous task of this user, and parent_task_id for the new one."""
    now = _aware(task.created_at or _now())
    prev = session.scalar(
        select(Task)
        .where(Task.user_id == user.id, Task.id != task.id, Task.created_at >= now - LOOKBACK)
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    written: list[str] = []
    if prev is None:
        return written
    # The gap runs from the end of the previous run when it is known, else from its start.
    since = _aware(prev.finished_at or prev.created_at)
    gap = max(timedelta(0), now - since)
    rated = _rated(session, prev.id)
    kind = classify_followup(prev.request_text, task.request_text, gap=gap, prev_rated=rated)
    target = prev
    if kind == "continue":
        target = checkpoint_parent(session, user, task) or prev
        task.parent_task_id = target.id
    elif kind in {"retry", "reformulate"}:
        task.parent_task_id = prev.id
    if kind is not None:
        payload: dict[str, Any] = {"gap_s": int(gap.total_seconds())}
        if kind in {"retry", "reformulate", "next_unrelated"}:
            payload["similarity"] = round(similarity(prev.request_text, task.request_text), 2)
        payload["rated"] = rated
        record(session, user.id, target.id, kind, payload)
        written.append(kind)
    if kind != "continue" and not rated and gap > ABANDON_AFTER and not _has(session, prev.id, "abandon"):
        record(session, user.id, prev.id, "abandon", {"via": "timeout", "after_min": int(gap.total_seconds() // 60)})
        written.append("abandon")
    session.flush()
    if written:
        _refresh(session, target if kind == "continue" else prev)
    return written


def on_dismiss(session: Session, user: User, task: Task) -> bool:
    """The rating card closed with ✕ and no rating: abandon (once)."""
    if _rated(session, task.id) or _has(session, task.id, "abandon"):
        return False
    record(session, user.id, task.id, "abandon", {"via": "dismiss"})
    _refresh(session, task)
    return True


# --- run signals -------------------------------------------------------------------------


def _ok(step: TaskStep) -> bool:
    return not step.error and not (step.result_text or "").lstrip().startswith("Ошибка")


def _method(args: dict | None) -> str:
    args = args or {}
    return str(args.get("method") or "GET").strip().upper()


def _host(args: dict | None) -> str:
    url = str((args or {}).get("url") or "")
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _family(name: str, args: dict | None) -> str:
    if name == "http_request":
        return _host(args)
    return name.split("_", 1)[0].lower()


def _is_write(name: str | None, args: dict | None) -> bool:
    if not name or name.startswith(("knowledge_", "memory_", "browser_", "mail_")):
        return False
    if name == "http_request":
        return _method(args) not in _READ_METHODS
    if name == "direct_api":
        return str((args or {}).get("method") or "get").lower() != "get"
    return name.endswith(("_write", "_save", "_create", "_update", "_add", "_set"))


def _is_read(name: str | None, args: dict | None) -> bool:
    if not name:
        return False
    if name == "http_request":
        return _method(args) in _READ_METHODS
    if name == "direct_api":
        return str((args or {}).get("method") or "get").lower() == "get"
    return name.endswith("_read") and not name.startswith(("browser_", "mail_"))


def _same_service(write_family: str, read_family: str) -> bool:
    if not write_family or not read_family:
        return False
    return write_family == read_family or read_family in write_family or write_family in read_family


def verify_results(steps: list[TaskStep]) -> list[tuple[str, dict]]:
    """verify_ok / verify_fail: the first read of the same service after each successful write."""
    out: list[tuple[str, dict]] = []
    for index, step in enumerate(steps):
        if not _is_write(step.tool_name, step.tool_args) or not _ok(step):
            continue
        family = _family(step.tool_name or "", step.tool_args)
        for later in steps[index + 1:]:
            if _is_write(later.tool_name, later.tool_args):
                break
            if _is_read(later.tool_name, later.tool_args) and _same_service(family, _family(later.tool_name or "", later.tool_args)):
                kind = "verify_ok" if _ok(later) else "verify_fail"
                out.append((kind, {"write_step": step.position, "read_step": later.position, "tool": later.tool_name}))
                break
    return out


def has_mail(steps: list[TaskStep]) -> bool:
    """The task wrote a letter: mail_send, or a confirmation about sending one."""
    for step in steps:
        if step.tool_name == "mail_send":
            return True
        if step.tool_name == "ask_confirmation":
            if _MAIL_WORDS.search(json.dumps(step.tool_args or {}, ensure_ascii=False)):
                return True
    return False


def on_run_end(session: Session, task: Task) -> list[str]:
    """Recompute the run signals of a task (idempotent: a later finish replaces them)."""
    for row in session.scalars(
        select(TaskSignal).where(TaskSignal.task_id == task.id, TaskSignal.kind.in_(RUN_KINDS))
    ):
        session.delete(row)
    session.flush()
    steps = list(
        session.scalars(select(TaskStep).where(TaskStep.task_id == task.id).order_by(TaskStep.position))
    )
    written: list[str] = []
    if task.stopped in STOP_KINDS:
        record(session, task.user_id, task.id, task.stopped, {})
        written.append(task.stopped)
    for step in steps:
        if step.confirmation in {"approved", "rejected", "edited"}:
            kind = f"confirm_{step.confirmation}"
            record(session, task.user_id, task.id, kind, {"step": step.position, "tool": step.tool_name})
            written.append(kind)
    for kind, payload in verify_results(steps):
        record(session, task.user_id, task.id, kind, payload)
        written.append(kind)
    return written


def _refresh(session: Session, task: Task) -> None:
    from app.trajectories import TrajectoryError, refresh_document

    if task.outcome is None:
        return
    try:
        refresh_document(session, task)
    except TrajectoryError as exc:
        log.error("trajectory not refreshed task=%s code=%s", task.id, exc.code)


def safe(fn, *args) -> Any:
    """Signals never break a task: a failure is logged and rolled back to a savepoint."""
    session: Session = args[0]
    try:
        with session.begin_nested():
            return fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("signal failed fn=%s", getattr(fn, "__name__", fn))
        return None
