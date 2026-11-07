"""Read-only health check of the self-learning signals (`python -m app.cli learning-check`).

Prints only counts and the /admin/quality summary: no task texts, no emails, no user ids, no tokens.
Nothing is written: the session is rolled back at the end and no schema is created.
Used by the deploy-pinvds workflow (mode=learning-check) to check the run journal, the SFT export
and /admin/quality on the live server.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.models import Task, TaskFeedback, TaskSignal, Trajectory, User
from app.quality import quality_summary
from app.trajectories import confirmed_good, internal_emails, list_documents, to_dpo, to_sft

# A task with neither an outcome nor finished_at this long after start was never closed by the run.
STUCK_AFTER = timedelta(hours=1)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
FORBIDDEN_KEYS = {"user_id", "email", "password", "token"}


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(str(key))
            found |= _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= _keys(inner)
    return found


def _export_check(lines: list[dict], account_emails: set[str]) -> dict:
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    keys = set().union(*(_keys(line) for line in lines)) if lines else set()
    lowered = body.lower()
    return {
        "lines": len(lines),
        "bytes": len(body.encode()),
        "forbidden_keys": sorted(keys & FORBIDDEN_KEYS),
        # Account emails must never reach the dataset (masking turns emails into placeholders).
        "account_emails_found": sum(1 for email in account_emails if email and email in lowered),
        "raw_emails_found": len(EMAIL_RE.findall(body)),
    }


def learning_check(session: Session, *, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    bind = session.get_bind()
    report: dict[str, Any] = {"checked_at": now.isoformat(), "days": days}

    try:
        report["alembic_version"] = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        session.rollback()
        report["alembic_version"] = None
    inspector = inspect(bind)
    task_columns = {col["name"] for col in inspector.get_columns("tasks")}
    report["schema"] = {
        "task_signals_table": inspector.has_table("task_signals"),
        "tasks_journal_columns": sorted(task_columns & {"finished_at", "stopped", "duration_ms", "parent_task_id"}),
    }

    users = session.execute(select(User.email, User.consent_training, User.is_admin)).all()
    internal = internal_emails()
    report["users"] = {
        "total": len(users),
        "consent_training": sum(1 for _, consent, _ in users if consent),
        "admins": sum(1 for _, _, admin in users if admin),
        "internal": sum(1 for email, _, _ in users if (email or "").strip().lower() in internal),
        "external_with_consent": sum(
            1 for email, consent, _ in users if consent and (email or "").strip().lower() not in internal
        ),
    }

    tasks = session.execute(
        select(Task.created_at, Task.finished_at, Task.outcome, Task.stopped, Task.duration_ms, Task.parent_task_id)
    ).all()
    recent = [row for row in tasks if _aware(row.created_at) >= since]
    stuck_before = now - STUCK_AFTER

    def journal(rows: list) -> dict:
        return {
            "tasks": len(rows),
            "with_outcome": sum(1 for r in rows if r.outcome),
            "outcomes": dict(Counter(r.outcome or "none" for r in rows)),
            "with_finished_at": sum(1 for r in rows if r.finished_at is not None),
            "with_duration": sum(1 for r in rows if r.duration_ms is not None),
            "with_parent": sum(1 for r in rows if r.parent_task_id is not None),
            "stopped": dict(Counter(r.stopped for r in rows if r.stopped)),
            # Older than STUCK_AFTER and still open: the run never closed the task.
            "never_closed": sum(
                1 for r in rows if r.finished_at is None and r.outcome is None and _aware(r.created_at) < stuck_before
            ),
            # Has an outcome but no journal: closed by an old build or by the rating only.
            "outcome_without_journal": sum(1 for r in rows if r.outcome and r.finished_at is None),
            "last_task_at": max((_aware(r.created_at) for r in rows), default=None),
            "last_finished_at": max((_aware(r.finished_at) for r in rows if r.finished_at), default=None),
        }

    report["journal_all"] = journal(tasks)
    report["journal_recent"] = journal(recent)
    for part in ("journal_all", "journal_recent"):
        for key in ("last_task_at", "last_finished_at"):
            value = report[part][key]
            report[part][key] = value.isoformat() if value else None

    feedback = session.execute(
        select(TaskFeedback.rating, TaskFeedback.acceptance, TaskFeedback.reason_code, TaskFeedback.user_fixed)
    ).all()
    report["feedback"] = {
        "rows": len(feedback),
        "thumbs_up": sum(1 for f in feedback if f.rating == 1),
        "thumbs_down": sum(1 for f in feedback if f.rating == -1),
        "with_reason_code": sum(1 for f in feedback if f.reason_code),
        "user_fixed": sum(1 for f in feedback if f.user_fixed),
    }
    report["signals_all"] = dict(Counter(session.scalars(select(TaskSignal.kind))))
    report["signals_without_task"] = int(
        session.scalar(select(func.count()).select_from(TaskSignal).where(TaskSignal.task_id.is_(None))) or 0
    )

    trajectories = list(session.scalars(select(Trajectory)))
    report["trajectories"] = {
        "documents": len(trajectories),
        "internal": sum(1 for t in trajectories if (t.payload or {}).get("internal")),
        "confirmed_good": sum(1 for t in trajectories if confirmed_good(t.payload or {})),
        "with_journal": sum(1 for t in trajectories if (t.payload or {}).get("stopped") is not None
                            or (t.payload or {}).get("duration_ms") is not None),
        "with_revision": sum(1 for t in trajectories if (t.payload or {}).get("revision")),
    }

    # The same selection as GET /v1/admin/trajectories.jsonl, counted, never printed.
    emails = {(email or "").strip().lower() for email, _, _ in users}
    raw = list_documents(session)
    sft = [to_sft(doc) for doc in list_documents(session, success_only=True, confirmed_only=True, exclude_internal=True)]
    dpo = [pair for doc in list_documents(session, exclude_internal=True) for pair in to_dpo(doc)]
    report["export"] = {
        "raw": _export_check(raw, emails),
        "sft": _export_check(sft, emails),
        "dpo": _export_check(dpo, emails),
    }

    report["quality"] = quality_summary(session, days=days)
    session.rollback()
    return report


def live_endpoints(session: Session, base_url: str, days: int = 7, http: Any = None) -> dict:
    """Call /v1/admin/quality and the SFT export on the running API as an admin. Prints statuses and counts."""
    import httpx

    from app.auth import issue_token

    admin = session.scalar(select(User).where(User.is_admin.is_(True)).limit(1))
    session.rollback()
    if admin is None:
        return {"skipped": "no_admin_user"}
    token = issue_token(admin.id)
    headers = {"Authorization": f"Bearer {token}"}
    out: dict[str, Any] = {}
    client = http or httpx.Client(base_url=base_url, timeout=30)
    try:
        quality = client.get("/v1/admin/quality", params={"days": days}, headers=headers)
        out["quality_status"] = quality.status_code
        if quality.status_code == 200:
            data = quality.json()
            out["quality_tasks"] = data.get("tasks")
            out["quality_keys"] = sorted(data)
        else:
            out["quality_error"] = quality.text[:200]
        for kind in ("raw", "sft", "dpo"):
            resp = client.get("/v1/admin/trajectories.jsonl", params={"kind": kind}, headers=headers)
            lines = [line for line in resp.text.splitlines() if line.strip()] if resp.status_code == 200 else []
            bad = 0
            for line in lines:
                try:
                    json.loads(line)
                except ValueError:
                    bad += 1
            out[f"export_{kind}"] = {"status": resp.status_code, "lines": len(lines), "bad_json": bad}
    finally:
        if http is None:
            client.close()
    return out
