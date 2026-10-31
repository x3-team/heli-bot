"""Task trajectories for a future fine-tune.

Records stay in Postgres (the Russia server). A document is written only when
the user has opted in. Exports are anonymized and contain public model aliases.
There is no training job in v0.1.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.aliases import PUBLIC_MODEL_ALIASES
from app.config import get_settings
from app.masking import mask_text, mask_value, redact_secrets, redact_secrets_text
from app.models import Task, TaskFeedback, TaskStep, Trajectory, User

log = logging.getLogger(__name__)

OUTCOMES = {"success", "fail", "aborted"}
ACCEPTANCE = {"accepted", "edited", "rejected"}
OBSERVATIONS = {"dom", "screenshot", "none"}
CONFIRMATIONS = {"approved", "edited", "rejected"}
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
    task.final_answer = final_answer
    session.flush()
    refresh_document(session, task)
    log.info("task finished id=%s outcome=%s", task.id, outcome)
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
) -> TaskFeedback:
    if rating is not None and rating not in {1, -1}:
        raise TrajectoryError("bad_rating")
    if acceptance is not None and acceptance not in ACCEPTANCE:
        raise TrajectoryError("bad_acceptance")
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
        row.user_edit = user_edit
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


def refresh_document(session: Session, task: Task) -> Trajectory | None:
    user = session.get(User, task.user_id)
    steps = list(
        session.scalars(
            select(TaskStep).where(TaskStep.task_id == task.id).order_by(TaskStep.position)
        )
    )
    feedback_row = session.scalar(select(TaskFeedback).where(TaskFeedback.task_id == task.id))
    if user is None or not user.consent_training:
        existing = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
        if existing is not None:
            session.delete(existing)
        return None
    if task.outcome is None:
        return None
    payload = build_payload(task, user, steps, feedback_row)
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
) -> dict[str, Any]:
    feedback_row = task.feedback if feedback_row is None else feedback_row
    feedback = None
    if feedback_row is not None:
        feedback = {
            "rating": feedback_row.rating,
            "acceptance": feedback_row.acceptance,
            "comment": mask_text(feedback_row.comment or "") or None,
            "user_edit": mask_text(feedback_row.user_edit or "") or None,
        }
    steps = []
    confirmations = []
    for step in steps_in if steps_in is not None else task.steps:
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
    }


def list_documents(
    session: Session,
    *,
    success_only: bool = False,
    thumbs_up_only: bool = False,
) -> list[dict[str, Any]]:
    rows = session.scalars(select(Trajectory).where(Trajectory.consent.is_(True))).all()
    documents = []
    for row in rows:
        payload = row.payload
        if success_only and payload.get("result") != "success":
            continue
        if thumbs_up_only:
            feedback = payload.get("feedback") or {}
            if feedback.get("rating") != 1:
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
