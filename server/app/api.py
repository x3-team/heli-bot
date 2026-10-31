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
from app.browser import (
    BrowserError,
    attach_local,
    detach_local,
    get_browser_driver,
    pool_start,
    pool_status,
    pool_stop,
)
from app.vault import (
    VaultError,
    delete_credential,
    fill_for_model,
    list_credentials,
    public_credential,
    store_credential,
)
from app.config import get_settings
from app.db import get_db
from app.models import TaskStep, User
from app.llm.client import HeliClient, HeliError
from app.llm.run import run_turn
from app.quota import QuotaError, public_usage, snapshot
from app.schedules import ScheduleError, create_schedule, delete_schedule, list_schedules, run_due
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
    "local_browser_required": 409,
    "bad_browser": 400,
    "bad_port": 400,
    "cloud_running": 409,
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


@router.get("/browser")
def browser_status(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    driver=Depends(get_browser_driver),
) -> dict:
    return pool_status(session, user.id, driver)


class LocalBrowserBody(BaseModel):
    browser: str = Field(min_length=1, max_length=16)
    port: int = Field(ge=1, le=65535)


@router.post("/browser/local/attach")
def browser_local_attach(
    body: LocalBrowserBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        return attach_local(session, user.id, body.browser.strip().lower(), body.port)
    except BrowserError as exc:
        status = _STATUS.get(exc.code, 409)
        raise HTTPException(status_code=status, detail=exc.code) from None


@router.post("/browser/local/detach")
def browser_local_detach(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    return detach_local(session, user.id)


@router.post("/browser/start")
def browser_start(
    prefer_local: bool = True,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    driver=Depends(get_browser_driver),
) -> dict:
    try:
        return pool_start(session, user.id, driver, prefer_local=prefer_local)
    except BrowserError as exc:
        status = _STATUS.get(exc.code, 503 if exc.code == "browser_unavailable" else 409)
        raise HTTPException(status_code=status, detail=exc.code) from None


@router.post("/browser/stop")
def browser_stop(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    driver=Depends(get_browser_driver),
) -> dict:
    try:
        return pool_stop(session, user.id, driver)
    except BrowserError as exc:
        status = 503 if exc.code == "browser_unavailable" else 409
        raise HTTPException(status_code=status, detail=exc.code) from None


class CredentialBody(BaseModel):
    site: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=1, max_length=256)
    secret: str = Field(min_length=1, max_length=4096)


def _vault_fail(exc: VaultError) -> None:
    status = 503 if exc.code == "vault_unavailable" else 404
    raise HTTPException(status_code=status, detail=exc.code)


@router.get("/credentials")
def credentials_list(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    return {"credentials": list_credentials(session, user)}


@router.post("/credentials", status_code=201)
def credentials_create(
    body: CredentialBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        row = store_credential(session, user, body.site, body.username, body.secret)
    except VaultError as exc:
        _vault_fail(exc)
    return public_credential(row)


@router.delete("/credentials/{credential_id}")
def credentials_delete(
    credential_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        delete_credential(session, user, credential_id)
    except VaultError as exc:
        _vault_fail(exc)
    return {"deleted": True}


@router.post("/credentials/{credential_id}/fill")
def credentials_fill(
    credential_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        placeholder = fill_for_model(session, user, credential_id)
    except VaultError as exc:
        _vault_fail(exc)
    return {"placeholder": placeholder}


class ScheduleBody(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    expression: str = Field(min_length=1, max_length=80)
    request: str = Field(min_length=1, max_length=4000)


@router.get("/schedules")
def schedules_list(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    return {"schedules": list_schedules(session, user)}


@router.post("/schedules", status_code=201)
def schedules_create(
    body: ScheduleBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        row = create_schedule(session, user, body.label, body.expression, body.request)
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from None
    return {
        "id": str(row.id),
        "label": row.label,
        "expression": row.expression,
        "enabled": row.enabled,
        "next_run": row.next_run.isoformat(),
        "last_result": row.last_result,
        "last_error": row.last_error,
    }


@router.delete("/schedules/{schedule_id}")
def schedules_delete(
    schedule_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        delete_schedule(session, user, schedule_id)
    except ScheduleError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from None
    return {"deleted": True}


@router.post("/schedules/run")
def schedules_run(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    client: HeliClient = Depends(get_heli_client),
    driver=Depends(get_browser_driver),
) -> dict:
    ran = run_due(session, client, driver, user_id=user.id)
    return {"ran": ran, "schedules": list_schedules(session, user)}


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
