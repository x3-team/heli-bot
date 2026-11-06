"""Task trajectories for a future fine-tune.

Records stay in Postgres (the Russia server). A document is written only when
the user has opted in. Exports are anonymized and contain public model aliases.
There is no training job in v0.1.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.aliases import PUBLIC_MODEL_ALIASES
from app.config import get_settings
from app.masking import mask_text, mask_value, redact_secrets, redact_secrets_text
from app.models import Task, TaskFeedback, TaskSignal, TaskStep, Trajectory, UsageLedger, User

log = logging.getLogger(__name__)

OUTCOMES = {"success", "fail", "aborted"}
ACCEPTANCE = {"accepted", "edited", "rejected"}
OBSERVATIONS = {"dom", "screenshot", "none"}
CONFIRMATIONS = {"approved", "edited", "rejected"}
# One-tap reasons after 👎 (UI labels in packages/ui: «Неверные данные», «Сделал не всё», …).
REASON_CODES = {"wrong_fact", "incomplete", "wrong_system", "refused_needed", "dangerous", "slow", "other"}
MAIL_DISPOSITIONS = {"sent_as_is", "edited_then_sent", "declined"}
SCREENSHOT_LIMIT = 2_000_000


class TrajectoryError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def user_hash(user_id: uuid.UUID) -> str:
    pepper = get_settings().trajectory_hash_pepper
    if not pepper:
        raise TrajectoryError("hash_pepper_missing")
    digest = hashlib.sha256(f"{pepper}:{user_id}".encode()).hexdigest()
    return digest


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_alias(alias: str | None) -> str | None:
    if alias is None or alias == "":
        return None
    if alias not in PUBLIC_MODEL_ALIASES:
        raise TrajectoryError("unknown_alias")
    return alias


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def internal_emails() -> set[str]:
    """Accounts whose tasks never go into the SFT/DPO sets: the shared open-access account and the
    internal training accounts. Works with HELI_OPEN_ACCESS_EMAIL alone and with HELI_TRAINING_EMAILS."""
    settings = get_settings()
    emails = {_norm_email(settings.open_access_email)}
    raw = getattr(settings, "training_emails", "") or os.environ.get("HELI_TRAINING_EMAILS", "")
    emails |= {_norm_email(part) for part in str(raw).split(",") if part.strip()}
    emails.discard("")
    return emails


def is_internal(user: User | None) -> bool:
    return user is not None and _norm_email(user.email) in internal_emails()


def revision() -> str | None:
    for name in ("APP_REVISION", "GIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value[:64]
    return None


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _mark_finished(task: Task, stopped: str | None) -> None:
    """Run journal: the first finish wins (a later rating does not move the end of the run)."""
    if task.finished_at is not None:
        return
    now = _now()
    task.finished_at = now
    task.stopped = (stopped or None) and str(stopped)[:32]
    if task.created_at is not None:
        task.duration_ms = max(0, int((now - _aware(task.created_at)).total_seconds() * 1000))


def start_task(session: Session, user: User, request_text: str, context: dict | None) -> Task:
    if not user.consent_pdn:
        raise TrajectoryError("pdn_consent_required")
    task = Task(
        user_id=user.id,
        request_text=redact_secrets_text(request_text),
        context=redact_secrets(context or {}),
        outcome=None,
        final_answer=None,
        created_at=_now(),
    )
    session.add(task)
    session.flush()
    from app import signals

    signals.safe(signals.on_task_start, session, user, task)
    log.info("task started id=%s", task.id)
    return task


def add_step(
    session: Session,
    user: User,
    task_id: uuid.UUID,
    *,
    thought: str | None = None,
    plan: str | None = None,
    model_alias: str | None = None,
    reply: str | None = None,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_args_edit: dict | None = None,
    result_text: str | None = None,
    observation_type: str | None = None,
    observation_text: str | None = None,
    screenshot_ref: str | None = None,
    screenshot: bytes | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_rub: Decimal | int | float | str = 0,
    latency_ms: int | None = None,
    error: str | None = None,
    attempt: int = 1,
    confirmation: str | None = None,
) -> TaskStep:
    task = _own_task(session, user, task_id)
    alias = _check_alias(model_alias)
    taken = session.scalar(
        select(func.count()).select_from(TaskStep).where(TaskStep.task_id == task.id)
    )
    position = int(taken or 0) + 1
    if observation_type is not None and observation_type not in OBSERVATIONS:
        raise TrajectoryError("bad_observation")
    if confirmation is not None and confirmation not in CONFIRMATIONS:
        raise TrajectoryError("bad_confirmation")
    if screenshot is not None and len(screenshot) > SCREENSHOT_LIMIT:
        raise TrajectoryError("screenshot_too_large")
    step = TaskStep(
        task_id=task.id,
        position=position,
        attempt=attempt,
        thought=redact_secrets_text(thought or "") or None,
        plan=redact_secrets_text(plan or "") or None,
        model_alias=alias,
        reply=redact_secrets_text(reply or "") or None,
        tool_name=tool_name,
        tool_args=redact_secrets(tool_args) if tool_args is not None else None,
        tool_args_edit=redact_secrets(tool_args_edit) if tool_args_edit is not None else None,
        result_text=redact_secrets_text(result_text or "") or None,
        observation_type=observation_type,
        observation_text=redact_secrets_text(observation_text or "") or None,
        screenshot_ref=redact_secrets_text(screenshot_ref) if screenshot_ref else None,
        screenshot=screenshot,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_rub=_money(cost_rub),
        latency_ms=latency_ms,
        error=redact_secrets_text(error or "") or None,
        confirmation=confirmation,
        created_at=_now(),
    )
    session.add(step)
    session.flush()
    log.info("task step id=%s position=%s alias=%s", task.id, position, alias or "-")
    return step


def finish_task(
    session: Session,
    user: User,
    task_id: uuid.UUID,
    outcome: str,
    final_answer: str | None,
) -> Task:
    if outcome not in OUTCOMES:
        raise TrajectoryError("bad_outcome")
    task = _own_task(session, user, task_id)
    task.outcome = outcome
    if final_answer:
        # A rating after a stopped or failed run sends no answer: keep the one the run left.
        task.final_answer = final_answer
    if task.finished_at is None:
        _mark_finished(task, "stop" if outcome == "aborted" else None)
        from app import signals

        signals.safe(signals.on_run_end, session, task)
    session.flush()
    refresh_document(session, task)
    log.info("task finished id=%s outcome=%s", task.id, outcome)
    return task


def auto_finish(
    session: Session,
    task_id: uuid.UUID,
    outcome: str,
    final_answer: str | None,
    *,
    stopped: str | None = None,
) -> Task | None:
    """Close a run the user has not rated yet, so every task reaches the dataset.

    A later rating from the user (finish_task) overrides this outcome. An auto «success» alone is not a
    training example: the SFT export also wants 👍, «принято» or a verify_ok signal (list_documents).
    """
    if outcome not in OUTCOMES:
        raise TrajectoryError("bad_outcome")
    task = session.get(Task, task_id)
    if task is None:
        return task
    from app import signals

    if task.finished_at is None:
        _mark_finished(task, stopped)
        signals.safe(signals.on_run_end, session, task)
    if task.outcome is not None:
        session.flush()
        try:
            refresh_document(session, task)
        except TrajectoryError as exc:
            log.error("trajectory not written task=%s code=%s", task.id, exc.code)
        return task
    task.outcome = outcome
    if final_answer:
        task.final_answer = redact_secrets_text(final_answer)
    session.flush()
    try:
        refresh_document(session, task)
    except TrajectoryError as exc:
        # A missing hash pepper must not break the run; the outcome is still kept.
        log.error("trajectory not written task=%s code=%s (dataset loses this task)", task.id, exc.code)
    log.info("task auto-finished id=%s outcome=%s", task.id, outcome)
    return task


def rate_task(
    session: Session,
    user: User,
    task_id: uuid.UUID,
    *,
    rating: int | None = None,
    acceptance: str | None = None,
    comment: str | None = None,
    user_edit: str | None = None,
    reason_code: str | None = None,
    user_fixed: bool | None = None,
    mail_disposition: str | None = None,
) -> TaskFeedback:
    if rating is not None and rating not in {1, -1}:
        raise TrajectoryError("bad_rating")
    if acceptance is not None and acceptance not in ACCEPTANCE:
        raise TrajectoryError("bad_acceptance")
    if reason_code is not None and reason_code not in REASON_CODES:
        raise TrajectoryError("bad_reason_code")
    if mail_disposition is not None and mail_disposition not in MAIL_DISPOSITIONS:
        raise TrajectoryError("bad_mail_disposition")
    if user_fixed:
        # «Исправил сам»: the bot's answer was not it; user_edit is how it should be (a DPO pair).
        acceptance = acceptance or "edited"
        rating = -1 if rating is None else rating
    task = _own_task(session, user, task_id)
    row = task.feedback
    if row is None:
        row = TaskFeedback(task_id=task.id, created_at=_now())
        session.add(row)
        task.feedback = row
    if rating is not None:
        row.rating = rating
    if acceptance is not None:
        row.acceptance = acceptance
    if comment is not None:
        row.comment = comment
    if user_edit is not None:
        row.user_edit = redact_secrets_text(user_edit)
    if reason_code is not None:
        row.reason_code = reason_code
    if user_fixed is not None:
        row.user_fixed = user_fixed
    if mail_disposition is not None:
        row.mail_disposition = mail_disposition
    session.flush()
    refresh_document(session, task)
    return row


def set_consents(session: Session, user: User, *, pdn: bool | None, training: bool | None) -> User:
    if pdn is not None:
        user.consent_pdn = pdn
    if training is not None:
        user.consent_training = training
    if user.consent_training is False:
        for row in session.scalars(select(Trajectory).where(Trajectory.user_id == user.id)):
            session.delete(row)
    elif user.consent_training:
        for task in session.scalars(select(Task).where(Task.user_id == user.id)):
            if task.outcome is not None:
                refresh_document(session, task)
    session.flush()
    return user


def delete_my_trajectories(session: Session, user: User) -> int:
    rows = list(session.scalars(select(Trajectory).where(Trajectory.user_id == user.id)))
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def delete_my_tasks(session: Session, user: User, *, keep: set[uuid.UUID] | None = None) -> dict[str, int]:
    """Delete the user's task journal: tasks, steps, ratings, signals and training documents.

    Credentials and memory stay. Spend rows stay for billing, without the link to the task.
    `keep`: tasks still running (their run would fail on a missing row).
    """
    keep = keep or set()
    ids = [tid for tid in session.scalars(select(Task.id).where(Task.user_id == user.id)) if tid not in keep]
    counts = {"tasks": 0, "steps": 0, "feedback": 0, "signals": 0, "trajectories": 0}
    for row in session.scalars(select(TaskSignal).where(TaskSignal.user_id == user.id)):
        if row.task_id is None or row.task_id not in keep:
            session.delete(row)
            counts["signals"] += 1
    if ids:
        for row in session.scalars(select(Trajectory).where(Trajectory.task_id.in_(ids))):
            session.delete(row)
            counts["trajectories"] += 1
        for row in session.scalars(select(TaskFeedback).where(TaskFeedback.task_id.in_(ids))):
            session.delete(row)
            counts["feedback"] += 1
        for row in session.scalars(select(TaskStep).where(TaskStep.task_id.in_(ids))):
            session.delete(row)
            counts["steps"] += 1
        for row in session.scalars(select(UsageLedger).where(UsageLedger.task_id.in_(ids))):
            row.task_id = None
        for row in session.scalars(select(Task).where(Task.parent_task_id.in_(ids))):
            row.parent_task_id = None
        session.flush()
        for row in session.scalars(select(Task).where(Task.id.in_(ids))):
            session.delete(row)
            counts["tasks"] += 1
    session.flush()
    log.info("task history deleted tasks=%s", counts["tasks"])
    return counts


def refresh_document(session: Session, task: Task) -> Trajectory | None:
    user = session.get(User, task.user_id)
    steps = list(
        session.scalars(
            select(TaskStep).where(TaskStep.task_id == task.id).order_by(TaskStep.position)
        )
    )
    feedback_row = session.scalar(select(TaskFeedback).where(TaskFeedback.task_id == task.id))
    signal_rows = list(
        session.scalars(select(TaskSignal).where(TaskSignal.task_id == task.id).order_by(TaskSignal.created_at))
    )
    if user is None or not user.consent_training:
        if user is not None and task.outcome is not None:
            log.info("trajectory skipped task=%s: no training consent", task.id)
        existing = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
        if existing is not None:
            session.delete(existing)
        return None
    if task.outcome is None:
        return None
    payload = build_payload(task, user, steps, feedback_row, signal_rows)
    now = _now()
    row = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
    if row is None:
        row = Trajectory(
            task_id=task.id,
            user_id=user.id,
            user_hash=payload["user_hash"],
            consent=True,
            outcome=task.outcome,
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.user_hash = payload["user_hash"]
        row.consent = True
        row.outcome = task.outcome
        row.payload = payload
        row.updated_at = now
    session.flush()
    return row


def build_payload(
    task: Task,
    user: User,
    steps_in: list[TaskStep] | None = None,
    feedback_row: TaskFeedback | None = None,
    signal_rows: list[TaskSignal] | None = None,
) -> dict[str, Any]:
    feedback_row = task.feedback if feedback_row is None else feedback_row
    feedback = None
    if feedback_row is not None:
        feedback = {
            "rating": feedback_row.rating,
            "acceptance": feedback_row.acceptance,
            "comment": mask_text(feedback_row.comment or "") or None,
            "user_edit": mask_text(feedback_row.user_edit or "") or None,
            "reason_code": feedback_row.reason_code,
            "user_fixed": feedback_row.user_fixed,
            "mail_disposition": feedback_row.mail_disposition,
        }
    steps = []
    confirmations = []
    total = Decimal("0")
    for step in steps_in if steps_in is not None else task.steps:
        total += Decimal(step.cost_rub or 0)
        observation = None
        if step.observation_type:
            observation = {
                "type": step.observation_type,
                "ref": mask_text(step.screenshot_ref) if step.screenshot_ref else (
                    f"db://steps/{step.id}/screenshot" if step.screenshot else None
                ),
                "text": mask_text(step.observation_text or "") or None,
            }
        steps.append(
            {
                "position": step.position,
                "attempt": step.attempt,
                "role": "assistant",
                "model": step.model_alias,
                "thought": mask_text(step.thought or "") or None,
                "plan": mask_text(step.plan or "") or None,
                "reply": mask_text(step.reply or "") or None,
                "tool_call": (
                    {"name": step.tool_name, "args": mask_value(step.tool_args)}
                    if step.tool_name
                    else None
                ),
                "tool_args_edit": mask_value(step.tool_args_edit) if step.tool_args_edit else None,
                "result": mask_text(step.result_text or "") or None,
                "observation": observation,
                "input_tokens": step.input_tokens,
                "output_tokens": step.output_tokens,
                "cost_rub": format(step.cost_rub, "f"),
                "latency_ms": step.latency_ms,
                "error": mask_text(step.error or "") or None,
                "confirmation": step.confirmation,
            }
        )
        if step.confirmation:
            confirmations.append({"step": step.position, "decision": step.confirmation})
    return {
        "id": str(task.id),
        "ts": task.created_at.isoformat(),
        "user_hash": user_hash(user.id),
        "consent": True,
        "task": mask_text(task.request_text),
        "context": mask_value(task.context or {}),
        "steps": steps,
        "result": task.outcome,
        "final_answer": mask_text(task.final_answer or "") or None,
        "feedback": feedback,
        "confirmations": confirmations,
        "signals": [
            {"kind": row.kind, "payload": mask_value(row.payload or {})} for row in (signal_rows or [])
        ],
        "stopped": task.stopped,
        "duration_ms": task.duration_ms,
        "cost_rub_total": format(_money(total), "f"),
        "parent_id": str(task.parent_task_id) if task.parent_task_id else None,
        "revision": revision(),
        # The shared open-access / training account: raw export only, never SFT or DPO.
        "internal": is_internal(user),
    }


def confirmed_good(payload: dict[str, Any]) -> bool:
    """A training example needs a real «good»: 👍, «принято» or a passed check (verify_ok).

    An auto_finish «success» alone (the run simply did not stop) does not count.
    """
    feedback = payload.get("feedback") or {}
    if feedback.get("rating") == -1:
        return False
    if feedback.get("rating") == 1 or feedback.get("acceptance") == "accepted":
        return True
    return any(s.get("kind") == "verify_ok" for s in payload.get("signals") or [])


def _internal_user_ids(session: Session) -> set[uuid.UUID]:
    emails = internal_emails()
    if not emails:
        return set()
    return {
        uid for uid, email in session.execute(select(User.id, User.email)) if _norm_email(email) in emails
    }


def list_documents(
    session: Session,
    *,
    success_only: bool = False,
    thumbs_up_only: bool = False,
    confirmed_only: bool = False,
    exclude_internal: bool = False,
) -> list[dict[str, Any]]:
    rows = session.scalars(select(Trajectory).where(Trajectory.consent.is_(True))).all()
    internal_ids = _internal_user_ids(session) if exclude_internal else set()
    documents = []
    for row in rows:
        payload = row.payload
        if exclude_internal and (payload.get("internal") or row.user_id in internal_ids):
            continue
        if success_only and payload.get("result") != "success":
            continue
        if thumbs_up_only:
            feedback = payload.get("feedback") or {}
            if feedback.get("rating") != 1:
                continue
        if confirmed_only and not confirmed_good(payload):
            continue
        documents.append(payload)
    return documents


def to_sft(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("context") or {}
    user_content = payload.get("task") or ""
    if context:
        import json

        user_content = user_content + "\n\n" + json.dumps(context, ensure_ascii=False)
    return {
        "id": payload["id"],
        "messages": [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": payload.get("final_answer") or ""},
        ],
        "steps": payload.get("steps") or [],
        "outcome": payload.get("result"),
    }


def to_dpo(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    feedback = payload.get("feedback") or {}
    chosen = feedback.get("user_edit")
    rejected = payload.get("final_answer")
    edited = feedback.get("acceptance") == "edited" or feedback.get("rating") == -1
    if edited and chosen and rejected and chosen != rejected:
        pairs.append(
            {
                "id": payload["id"],
                "kind": "answer",
                "prompt": payload.get("task") or "",
                "chosen": chosen,
                "rejected": rejected,
            }
        )
    for step in payload.get("steps") or []:
        original = step.get("tool_call")
        edited_args = step.get("tool_args_edit")
        if step.get("confirmation") == "edited" and original and edited_args:
            pairs.append(
                {
                    "id": f"{payload['id']}:{step['position']}",
                    "kind": "action",
                    "prompt": step.get("thought") or payload.get("task") or "",
                    "chosen": edited_args,
                    "rejected": original.get("args"),
                }
            )
    return pairs


def _own_task(session: Session, user: User, task_id: uuid.UUID) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise TrajectoryError("not_found")
    return task
