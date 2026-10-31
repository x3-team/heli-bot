import base64
import binascii
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import AuthError, issue_login_code, issue_token, read_user_id, verify_login_code
from app.config import get_settings
from app.db import get_db
from app.models import TaskStep, User
from app.llm.client import HeliClient, HeliError
from app.llm.run import run_turn
from app.quota import QuotaError, public_usage, snapshot
from app.trajectories import (
    TrajectoryError,
    add_step,
    delete_my_trajectories,
    finish_task,
    list_documents,
    rate_task,
    set_consents,
    start_task,
    to_dpo,
    to_sft,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")

_STATUS = {
    "auth_not_configured": 503,
    "invite_invalid": 400,
    "code_invalid": 401,
    "token_invalid": 401,
    "pdn_consent_required": 403,
    "unknown_alias": 400,
    "not_found": 404,
    "hash_pepper_missing": 503,
    "screenshot_too_large": 413,
    "bad_observation": 400,
    "bad_confirmation": 400,
    "bad_outcome": 400,
    "bad_rating": 400,
    "bad_acceptance": 400,
    "bad_screenshot": 400,
    "limit_reached": 403,
}


def _fail(exc: AuthError | TrajectoryError | QuotaError) -> None:
    status = _STATUS.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.code)


def get_heli_client() -> HeliClient:
    try:
        return HeliClient.from_env()
    except HeliError as exc:
        status = 503 if exc.code == "missing_api_key" else 502
        raise HTTPException(status_code=status, detail=exc.code) from None


def get_current_user(
    session: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="token_invalid")
    token = authorization.split(" ", 1)[1].strip()
    try:
        user_id = read_user_id(token)
    except AuthError as exc:
        _fail(exc)
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="token_invalid")
    return user


class CodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    invite_code: str = Field(min_length=4, max_length=128)


class CodeVerify(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=4, max_length=12)


class ConsentBody(BaseModel):
    consent_pdn: bool | None = None
    consent_training: bool | None = None


class TaskBody(BaseModel):
    request: str = Field(min_length=1)
    context: dict = Field(default_factory=dict)


class StepBody(BaseModel):
    thought: str | None = None
    plan: str | None = None
    model_alias: str | None = None
    reply: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_args_edit: dict | None = None
    result_text: str | None = None
    observation_type: str | None = None
    observation_text: str | None = None
    screenshot_ref: str | None = None
    screenshot_base64: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_rub: Decimal = Decimal("0")
    latency_ms: int | None = None
    error: str | None = None
    attempt: int = 1
    confirmation: str | None = None


class FinishBody(BaseModel):
    outcome: str
    final_answer: str | None = None


class FeedbackBody(BaseModel):
    rating: int | None = None
    acceptance: str | None = None
    comment: str | None = None
    user_edit: str | None = None


@router.post("/auth/request-code", status_code=202)
def request_code(body: CodeRequest, request: Request, session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if settings.app_env != "test":
        # SMTP delivery is not wired. Operators issue a code with the CLI.
        raise HTTPException(status_code=501, detail="email_not_configured")
    try:
        code = issue_login_code(session, body.email, body.invite_code)
    except AuthError as exc:
        _fail(exc)
    if settings.app_env == "test":
        request.app.state.mail_sink.append(code)
    log.info("login code issued")
    return {"status": "sent"}


@router.post("/auth/verify")
def verify_code(body: CodeVerify, session: Session = Depends(get_db)) -> dict:
    try:
        user = verify_login_code(session, body.email, body.code)
        token = issue_token(user.id)
    except AuthError as exc:
        _fail(exc)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {
        "id": str(user.id),
        "consent_pdn": user.consent_pdn,
        "consent_training": user.consent_training,
    }


@router.put("/me/consents")
def update_consents(
    body: ConsentBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        set_consents(session, user, pdn=body.consent_pdn, training=body.consent_training)
    except TrajectoryError as exc:
        _fail(exc)
    return {"consent_pdn": user.consent_pdn, "consent_training": user.consent_training}


@router.delete("/me/trajectories")
def delete_trajectories(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    removed = delete_my_trajectories(session, user)
    return {"deleted": removed}


@router.post("/tasks", status_code=201)
def create_task(
    body: TaskBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        task = start_task(session, user, body.request, body.context)
    except TrajectoryError as exc:
        _fail(exc)
    return {"id": str(task.id)}


@router.post("/tasks/{task_id}/steps", status_code=201)
def create_step(
    task_id: uuid.UUID,
    body: StepBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    screenshot = None
    if body.screenshot_base64:
        raw = body.screenshot_base64.split(",", 1)[-1]
        try:
            screenshot = base64.b64decode(raw, validate=True)
        except binascii.Error as exc:
            raise HTTPException(status_code=400, detail="bad_screenshot") from exc
    try:
        step = add_step(
            session,
            user,
            task_id,
            thought=body.thought,
            plan=body.plan,
            model_alias=body.model_alias,
            reply=body.reply,
            tool_name=body.tool_name,
            tool_args=body.tool_args,
            tool_args_edit=body.tool_args_edit,
            result_text=body.result_text,
            observation_type=body.observation_type,
            observation_text=body.observation_text,
            screenshot_ref=body.screenshot_ref,
            screenshot=screenshot,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cost_rub=body.cost_rub,
            latency_ms=body.latency_ms,
            error=body.error,
            attempt=body.attempt,
            confirmation=body.confirmation,
        )
    except TrajectoryError as exc:
        _fail(exc)
    return {"id": str(step.id), "position": step.position, "model": step.model_alias}


class LimitBody(BaseModel):
    monthly_limit_rub: Decimal = Field(gt=0, le=Decimal("1000000"))
    weekly_limit_rub: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"))


@router.get("/usage")
def usage(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    return public_usage(snapshot(session, user))


@router.get("/admin/quotas")
def admin_quotas(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")
    from sqlalchemy import func, select

    people = []
    for person in session.scalars(select(User).order_by(User.created_at)):
        view = public_usage(snapshot(session, person))
        people.append({"id": str(person.id), "email": person.email, **view})
    totals = session.execute(
        select(TaskStep.task_id, func.sum(TaskStep.cost_rub))
        .group_by(TaskStep.task_id)
        .order_by(func.sum(TaskStep.cost_rub).desc())
        .limit(10)
    ).all()
    return {
        "users": people,
        "top_tasks": [{"id": str(task_id), "cost_rub": str(cost or 0)} for task_id, cost in totals],
    }


@router.patch("/admin/users/{user_id}/limit")
def set_user_limit(
    user_id: uuid.UUID,
    body: LimitBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="not_found")
    target.monthly_limit_rub = body.monthly_limit_rub
    if body.weekly_limit_rub is not None:
        target.weekly_limit_rub = body.weekly_limit_rub
    session.flush()
    return public_usage(snapshot(session, target))


@router.post("/tasks/{task_id}/run")
def run(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    client: HeliClient = Depends(get_heli_client),
) -> dict:
    from app.trajectories import _own_task

    try:
        task = _own_task(session, user, task_id)
        turn = run_turn(session, user, task, client)
    except TrajectoryError as exc:
        _fail(exc)
    except QuotaError as exc:
        _fail(exc)
    except HeliError as exc:
        status = 503 if exc.code == "missing_api_key" else 502
        raise HTTPException(status_code=status, detail=exc.code) from None
    return {
        "alias": turn.alias,
        "reply": turn.text,
        "task_type": turn.task_type,
        "complexity": turn.complexity,
    }


@router.post("/tasks/{task_id}/finish")
def finish(
    task_id: uuid.UUID,
    body: FinishBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        task = finish_task(session, user, task_id, body.outcome, body.final_answer)
    except TrajectoryError as exc:
        _fail(exc)
    return {"id": str(task.id), "outcome": task.outcome}


@router.post("/tasks/{task_id}/feedback")
def feedback(
    task_id: uuid.UUID,
    body: FeedbackBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        row = rate_task(
            session,
            user,
            task_id,
            rating=body.rating,
            acceptance=body.acceptance,
            comment=body.comment,
            user_edit=body.user_edit,
        )
    except TrajectoryError as exc:
        _fail(exc)
    return {"rating": row.rating, "acceptance": row.acceptance}


@router.get("/admin/trajectories.jsonl")
def export_trajectories(
    kind: str = "raw",
    success_only: bool | None = None,
    thumbs_up_only: bool = False,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> PlainTextResponse:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")
    if kind not in {"raw", "sft", "dpo"}:
        raise HTTPException(status_code=400, detail="bad_kind")
    if success_only is None:
        success_only = kind == "sft"
    try:
        documents = list_documents(
            session,
            success_only=success_only,
            thumbs_up_only=thumbs_up_only,
        )
    except TrajectoryError as exc:
        _fail(exc)
    import json

    lines: list[str] = []
    for document in documents:
        if kind == "sft":
            lines.append(json.dumps(to_sft(document), ensure_ascii=False))
        elif kind == "dpo":
            for pair in to_dpo(document):
                lines.append(json.dumps(pair, ensure_ascii=False))
        else:
            lines.append(json.dumps(document, ensure_ascii=False))
    body = ("\n".join(lines) + "\n") if lines else ""
    return PlainTextResponse(body, media_type="application/x-ndjson")
