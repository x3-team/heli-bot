"""Quality dashboard (GET /v1/admin/quality): what the self-learning signals say about the last days."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KnowledgeNote, Task, TaskFeedback, TaskSignal, TaskStep, Trajectory, User
from app.trajectories import _internal_user_ids, confirmed_good, internal_emails


def _avg(values: list) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def quality_summary(session: Session, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(Task, User.email, User.consent_training).join(User, User.id == Task.user_id).where(Task.created_at >= since)
    ).all()
    ids = [task.id for task, _, _ in rows]
    feedback = {
        row.task_id: row
        for row in (session.scalars(select(TaskFeedback).where(TaskFeedback.task_id.in_(ids))) if ids else [])
    }
    step_count: dict = {}
    step_cost: dict = {}
    if ids:
        for task_id, count, cost in session.execute(
            select(TaskStep.task_id, func.count(), func.sum(TaskStep.cost_rub))
            .where(TaskStep.task_id.in_(ids))
            .group_by(TaskStep.task_id)
        ):
            step_count[task_id] = int(count or 0)
            step_cost[task_id] = Decimal(str(cost or 0))
    signals = Counter(
        session.scalars(select(TaskSignal.kind).where(TaskSignal.created_at >= since))
    )
    documents = list(session.scalars(select(Trajectory).where(Trajectory.task_id.in_(ids)))) if ids else []
    internal_ids = _internal_user_ids(session)
    internal = internal_emails()

    rated = [fb for fb in feedback.values() if fb.rating is not None or fb.acceptance is not None]
    reasons = Counter(fb.reason_code for fb in feedback.values() if fb.reason_code)
    mail = Counter(fb.mail_disposition for fb in feedback.values() if fb.mail_disposition)
    stopped = Counter(task.stopped for task, _, _ in rows if task.stopped)
    durations = [task.duration_ms for task, _, _ in rows if task.duration_ms is not None]
    total = len(rows)
    return {
        "days": days,
        "since": since.isoformat(),
        "tasks": total,
        "rated": len(rated),
        "rated_share": round(len(rated) / total, 3) if total else 0.0,
        "thumbs_up": sum(1 for fb in feedback.values() if fb.rating == 1),
        "thumbs_down": sum(1 for fb in feedback.values() if fb.rating == -1),
        "reason_codes": dict(reasons),
        "user_fixed": sum(1 for fb in feedback.values() if fb.user_fixed),
        "mail_disposition": dict(mail),
        "signals": dict(signals),
        "stopped": dict(stopped),
        "avg_steps": _avg([step_count.get(i, 0) for i in ids]),
        "avg_duration_ms": _avg(durations),
        "avg_cost_rub": _avg([float(step_cost.get(i, 0)) for i in ids]),
        "internal_tasks": sum(1 for _, email, _ in rows if (email or "").strip().lower() in internal),
        "dataset": {
            "tasks": total,
            "with_outcome": sum(1 for task, _, _ in rows if task.outcome is not None),
            "with_consent_training": sum(1 for _, _, consent in rows if consent),
            "documents": len(documents),
            # The same rule as GET /v1/admin/trajectories.jsonl?kind=sft: consent, success, a real «good»,
            # not the shared or a training account (by the account too: old documents lack the flag).
            "confirmed_good": sum(1 for doc in documents if doc.consent and confirmed_good(doc.payload)),
            "sft_ready": sum(
                1
                for doc in documents
                if doc.consent
                and doc.payload.get("result") == "success"
                and confirmed_good(doc.payload)
                and not doc.payload.get("internal")
                and doc.user_id not in internal_ids
            ),
        },
        "knowledge_candidates_pending": int(
            session.scalar(select(func.count()).select_from(KnowledgeNote).where(KnowledgeNote.origin == "candidate")) or 0
        ),
        "hash_pepper_set": bool(get_settings().trajectory_hash_pepper),
    }
