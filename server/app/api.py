import base64
import json
import binascii
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import (
    AuthError,
    create_invite,
    issue_login_code,
    issue_login_code_for_user,
    issue_recovery_code,
    issue_token,
    login_with_password,
    open_access_user,
    sync_admin,
    read_user_id,
    register_user,
    reset_password,
    trial_active,
    verify_login_code,
)
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
from app.computer import MAX_STEPS, ComputerError, HistoryItem, next_step
from app.config import get_settings
from app.db import get_db
from app.models import TaskStep, User
from app.llm.client import HeliClient, HeliError
from app.agent import runs
from app.agent.control import ClientResult
from app.agent.task_run import decode_screenshot
from app.llm.run import run_turn
from app.memory import MemoryError, delete_memory, list_memory, remember
from app.quota import QuotaError, estimate_turn_reserve, public_usage, snapshot
from app.schedules import ScheduleError, create_schedule, delete_schedule, list_schedules, run_due
from app.support import SupportError, create_incident
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
    "invite_required": 403,
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
    "email_taken": 409,
    "credentials_invalid": 401,
    "password_weak": 400,
    "user_not_found": 404,
    "bad_message": 400,
    "bad_goal": 400,
    "too_many_steps": 409,
    "pc_hands_disabled": 404,
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
    settings = get_settings()
    if not settings.open_access and user.email == settings.open_access_email.strip().lower():
        # Shared test account: its old tokens stop working once sign-up is back.
        raise HTTPException(status_code=401, detail="token_invalid")
    sync_admin(user)
    return user


class CodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    invite_code: str | None = Field(default=None, min_length=4, max_length=128)


class CodeVerify(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=4, max_length=12)


class RegisterBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    consent_pdn: bool = False
    consent_training: bool = False
    invite_code: str | None = Field(default=None, max_length=128)


class PasswordLoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class RecoverRequestBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class RecoverResetBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(min_length=8, max_length=256)


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


@router.post("/auth/register", status_code=201)
def register(body: RegisterBody, session: Session = Depends(get_db)) -> dict:
    try:
        user = register_user(
            session,
            body.email,
            body.password,
            consent_pdn=body.consent_pdn,
            consent_training=body.consent_training,
            invite_code=(body.invite_code or "").strip() or None,
        )
        token = issue_token(user.id)
    except AuthError as exc:
        _fail(exc)
    return {
        "access_token": token,
        "token_type": "bearer",
        "trial_active": trial_active(user),
    }


@router.post("/auth/login")
def login_password(body: PasswordLoginBody, session: Session = Depends(get_db)) -> dict:
    try:
        user = login_with_password(session, body.email, body.password)
        token = issue_token(user.id)
    except AuthError as exc:
        _fail(exc)
    return {"access_token": token, "token_type": "bearer", "trial_active": trial_active(user)}


@router.post("/auth/open")
def open_access(session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.open_access:
        raise HTTPException(status_code=404, detail="open_access_disabled")
    try:
        user = open_access_user(session, settings.open_access_email)
        token = issue_token(user.id)
    except AuthError as exc:
        _fail(exc)
    return {"access_token": token, "token_type": "bearer", "trial_active": trial_active(user)}


@router.post("/auth/recover/request", status_code=202)
def recover_request(body: RecoverRequestBody, request: Request, session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if settings.app_env != "test":
        raise HTTPException(status_code=501, detail="email_not_configured")
    try:
        code = issue_recovery_code(session, body.email)
    except AuthError as exc:
        _fail(exc)
    if settings.app_env == "test":
        request.app.state.mail_sink.append(code)
    log.info("recovery code issued")
    return {"status": "sent"}


@router.post("/auth/recover/reset")
def recover_reset(body: RecoverResetBody, session: Session = Depends(get_db)) -> dict:
    try:
        user = reset_password(session, body.email, body.code, body.password)
        token = issue_token(user.id)
    except AuthError as exc:
        _fail(exc)
    return {"access_token": token, "token_type": "bearer", "trial_active": trial_active(user)}


@router.post("/auth/request-code", status_code=202)
def request_code(body: CodeRequest, request: Request, session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if settings.app_env != "test":
        # SMTP delivery is not wired. Operators issue a code with the CLI.
        raise HTTPException(status_code=501, detail="email_not_configured")
    try:
        if body.invite_code:
            code = issue_login_code(session, body.email, body.invite_code)
        else:
            code = issue_login_code_for_user(session, body.email)
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
    return {
        "access_token": token,
        "token_type": "bearer",
        "trial_active": trial_active(user),
    }


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "is_admin": user.is_admin,
        "consent_pdn": user.consent_pdn,
        "consent_training": user.consent_training,
    }


@router.get("/auth/config")
def auth_config() -> dict:
    return {"invite_only": get_settings().invite_only}


class AdminInviteBody(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=50)


class AdminCodeBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    purpose: str = Field(default="login", pattern="^(login|reset)$")


@router.post("/admin/invites", status_code=201)
def admin_create_invite(
    body: AdminInviteBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    code = create_invite(session, max_uses=body.max_uses)
    log.info("invite created by admin max_uses=%s", body.max_uses)
    return {"invite_code": code, "max_uses": body.max_uses}


@router.post("/admin/codes", status_code=201)
def admin_issue_code(
    body: AdminCodeBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Login or password-reset code for a tester while mail is not wired."""
    _require_admin(user)
    try:
        if body.purpose == "reset":
            code = issue_recovery_code(session, body.email)
        else:
            code = issue_login_code_for_user(session, body.email)
    except AuthError as exc:
        _fail(exc)
    log.info("%s code issued by admin", body.purpose)
    return {"code": code, "purpose": body.purpose, "expires_in_minutes": 10}


@router.get("/admin/users")
def admin_users(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    from sqlalchemy import select

    people = session.scalars(select(User).order_by(User.created_at)).all()
    return {
        "users": [
            {
                "id": str(person.id),
                "email": person.email,
                "is_admin": person.is_admin,
                "created_at": _iso_utc(person.created_at),
                "trial_active": trial_active(person),
            }
            for person in people
        ]
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


@router.post("/tasks/estimate")
def estimate_task(body: TaskBody, user: User = Depends(get_current_user)) -> dict:
    reserve = estimate_turn_reserve(body.request)
    return {
        "reserve_rub": format(reserve, "f"),
        "note": "Оценка максимума за один шаг (классификация и ответ).",
    }


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


class MemoryBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.get("/memory")
def memory_list(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    return {"facts": list_memory(session, user)}


@router.post("/memory", status_code=201)
def memory_create(
    body: MemoryBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        row = remember(session, user, body.text)
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from None
    return {"id": str(row.id), "text": row.text}


@router.delete("/memory/{fact_id}")
def memory_delete(
    fact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        delete_memory(session, user, fact_id)
    except MemoryError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from None
    return {"deleted": True}


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
    view = public_usage(snapshot(session, user), trial_active=trial_active(user))
    view["trial_ends_at"] = _iso_utc(user.trial_ends_at)
    return view


def _iso_utc(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


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
        view = public_usage(snapshot(session, person), trial_active=trial_active(person))
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


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")


@router.get("/admin/feedback")
def admin_feedback(
    limit: int = 50,
    rated_only: bool = False,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Recent tasks with their outcome and the user's rating, newest first."""
    _require_admin(user)
    from sqlalchemy import func, select

    from app.models import Task, TaskFeedback, Trajectory

    query = (
        select(Task, TaskFeedback, User.email)
        .join(User, User.id == Task.user_id)
        .outerjoin(TaskFeedback, TaskFeedback.task_id == Task.id)
        .order_by(Task.created_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    if rated_only:
        query = query.where(TaskFeedback.id.is_not(None))
    rows = session.execute(query).all()
    ids = [task.id for task, _, _ in rows]
    costs = dict(
        session.execute(
            select(TaskStep.task_id, func.sum(TaskStep.cost_rub)).where(TaskStep.task_id.in_(ids)).group_by(TaskStep.task_id)
        ).all()
    ) if ids else {}
    steps = dict(
        session.execute(
            select(TaskStep.task_id, func.count()).where(TaskStep.task_id.in_(ids)).group_by(TaskStep.task_id)
        ).all()
    ) if ids else {}
    stored = set(session.scalars(select(Trajectory.task_id).where(Trajectory.task_id.in_(ids)))) if ids else set()
    tasks = []
    for task, fb, email in rows:
        tasks.append(
            {
                "id": str(task.id),
                "created_at": _iso_utc(task.created_at),
                "user": email,
                "request": task.request_text,
                "final_answer": task.final_answer,
                "outcome": task.outcome,
                "steps": int(steps.get(task.id, 0)),
                "cost_rub": str(costs.get(task.id) or 0),
                "in_training_set": task.id in stored,
                "feedback": (
                    {
                        "rating": fb.rating,
                        "acceptance": fb.acceptance,
                        "comment": fb.comment,
                        "user_edit": fb.user_edit,
                        "created_at": _iso_utc(fb.created_at),
                    }
                    if fb is not None
                    else None
                ),
            }
        )
    totals = {
        "tasks": session.scalar(select(func.count()).select_from(Task)) or 0,
        "rated": session.scalar(select(func.count()).select_from(TaskFeedback)) or 0,
        "thumbs_up": session.scalar(select(func.count()).select_from(TaskFeedback).where(TaskFeedback.rating == 1)) or 0,
        "thumbs_down": session.scalar(select(func.count()).select_from(TaskFeedback).where(TaskFeedback.rating == -1)) or 0,
        "trajectories": session.scalar(select(func.count()).select_from(Trajectory)) or 0,
    }
    return {"totals": totals, "tasks": tasks}


@router.get("/admin/incidents")
def admin_incidents(
    limit: int = 50,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Bug reports from the app, newest first. The screenshot is a separate download."""
    _require_admin(user)
    from sqlalchemy import select

    from app.models import IncidentReport

    rows = session.execute(
        select(IncidentReport, User.email)
        .join(User, User.id == IncidentReport.user_id)
        .order_by(IncidentReport.created_at.desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return {
        "reports": [
            {
                "id": str(row.id),
                "created_at": _iso_utc(row.created_at),
                "user": email,
                "message": row.message,
                "log_excerpt": row.log_excerpt,
                "screenshot": f"/v1/admin/incidents/{row.id}/screenshot" if row.screenshot else None,
            }
            for row, email in rows
        ]
    }


@router.get("/admin/incidents/{report_id}/screenshot")
def admin_incident_screenshot(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_admin(user)
    from fastapi.responses import Response

    from app.models import IncidentReport

    row = session.get(IncidentReport, report_id)
    if row is None or not row.screenshot:
        raise HTTPException(status_code=404, detail="not_found")
    kind = "image/jpeg" if row.screenshot[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(content=row.screenshot, media_type=kind)


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
    return public_usage(snapshot(session, target), trial_active=trial_active(target))


class RunBody(BaseModel):
    client_tools: list[str] = Field(default_factory=list, max_length=32)
    estimate_rub: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000"))
    autopause: bool = True


@router.post("/tasks/{task_id}/run", response_model=None)
def run(
    task_id: uuid.UUID,
    request: Request,
    body: RunBody | None = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    client: HeliClient = Depends(get_heli_client),
):
    """JSON: one blocking turn with server tools. Accept: text/event-stream: background run with a step stream."""
    from app.trajectories import _own_task

    try:
        task = _own_task(session, user, task_id)
    except TrajectoryError as exc:
        _fail(exc)
    if "text/event-stream" in request.headers.get("accept", ""):
        options = body or RunBody()
        session.commit()
        agent_run = runs.registry.start(
            task_id=task.id,
            user_id=user.id,
            session_factory=get_run_session_factory(),
            client=client,
            client_tools=options.client_tools,
            estimate_rub=options.estimate_rub,
            autopause=options.autopause,
        )
        return _sse(agent_run, 0)
    try:
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
        "stopped": turn.stopped,
    }


def get_run_session_factory():
    from app.db import get_session_factory

    return get_session_factory()


def _sse(agent_run: "runs.AgentRun", after: int) -> StreamingResponse:
    def body():
        for event in agent_run.stream(after):
            if event is None:
                yield ": ping\n\n"
                continue
            data = json.dumps(event, ensure_ascii=False, default=str)
            yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


def _own_run(task_id: uuid.UUID, user: User) -> "runs.AgentRun":
    agent_run = runs.registry.get(task_id, user.id)
    if agent_run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return agent_run


@router.get("/tasks/{task_id}/events", response_model=None)
def run_events(
    task_id: uuid.UUID,
    after: int = 0,
    last_event_id: str | None = Header(default=None),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    agent_run = _own_run(task_id, user)
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return _sse(agent_run, after)


class ControlBody(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop)$")


@router.post("/tasks/{task_id}/control")
def run_control(task_id: uuid.UUID, body: ControlBody, user: User = Depends(get_current_user)) -> dict:
    agent_run = _own_run(task_id, user)
    if body.action == "pause":
        agent_run.control.pause()
    elif body.action == "resume":
        agent_run.control.resume()
    else:
        agent_run.control.stop()
    return {"paused": agent_run.control.paused, "stopped": agent_run.control.stopped, "finished": agent_run.finished}


class ToolResultBody(BaseModel):
    call_id: str = Field(min_length=1, max_length=128)
    content: str = Field(default="", max_length=200_000)
    error: str | None = Field(default=None, max_length=200)
    screenshot_base64: str | None = Field(default=None, max_length=3_000_000)


@router.post("/tasks/{task_id}/tool-results")
def run_tool_result(task_id: uuid.UUID, body: ToolResultBody, user: User = Depends(get_current_user)) -> dict:
    agent_run = _own_run(task_id, user)
    delivered = agent_run.control.deliver(
        body.call_id,
        ClientResult(content=body.content, error=body.error, screenshot=decode_screenshot(body.screenshot_base64)),
    )
    if not delivered:
        raise HTTPException(status_code=409, detail="call_not_waiting")
    agent_run.forget_secret_args(body.call_id)
    return {"ok": True}
class ComputerHistoryBody(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    target: str | None = Field(default=None, max_length=300)
    ok: bool = True
    note: str | None = Field(default=None, max_length=500)


class ComputerStepBody(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    screenshot_base64: str = Field(min_length=1, max_length=3_000_000)
    history: list[ComputerHistoryBody] = Field(default_factory=list, max_length=MAX_STEPS)
    step: int = Field(default=1, ge=1, le=MAX_STEPS + 1)


@router.post("/tasks/{task_id}/computer/step")
def computer_step(
    task_id: uuid.UUID,
    body: ComputerStepBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    client: HeliClient = Depends(get_heli_client),
) -> dict:
    """One look at the user's screen: returns the next pointer/keyboard action."""
    try:
        step = next_step(
            session,
            user,
            task_id,
            client,
            goal=body.goal,
            screenshot_base64=body.screenshot_base64,
            history=[HistoryItem(**item.model_dump()) for item in body.history],
            step=body.step,
        )
    except ComputerError as exc:
        _fail(exc)
    except (TrajectoryError, QuotaError) as exc:
        _fail(exc)
    except HeliError as exc:
        status = 503 if exc.code == "missing_api_key" else 502
        raise HTTPException(status_code=status, detail=exc.code) from None
    return {
        "step_id": step.step_id,
        "action": step.action,
        "thought": step.thought,
        "risk": step.risk,
        "finished": step.finished,
        "error": step.error,
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


class IncidentBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    log_excerpt: str = Field(default="", max_length=32000)
    screenshot_base64: str | None = None


@router.post("/reports/incident", status_code=201)
def report_incident(
    body: IncidentBody,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    try:
        row = create_incident(
            session,
            user,
            message=body.message,
            log_excerpt=body.log_excerpt,
            screenshot_base64=body.screenshot_base64,
        )
    except SupportError as exc:
        status = _STATUS.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=exc.code) from None
    return {"id": str(row.id)}


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


# --- Heli's own browser on the server (the «Компьютер агента» panel) ---------------------

from fastapi.responses import Response  # noqa: E402

from app import cloud_browser  # noqa: E402

_CLOUD_STATUS = {"browser_busy": 429, "browser_unavailable": 503, "bad_input": 400, "bad_key": 400,
                 "bad_url": 400, "blocked_address": 400, "dns_failed": 400}


def _cloud_on() -> None:
    if not cloud_browser.ready():
        raise HTTPException(status_code=503, detail="cloud_browser_off")


@router.get("/cloud-browser")
def cloud_browser_status(user: User = Depends(get_current_user)) -> dict:
    if not cloud_browser.ready():
        return {
            "enabled": False,
            "running": False,
            "version": 0,
            "url": "",
            "title": "",
            "saved_logins": False,
            "error": cloud_browser.last_error(),
        }
    # The panel polls this while it is open: keep its screen live.
    cloud_browser.pool().watch(user.id)
    return {"enabled": True, **cloud_browser.pool().status(user.id)}


@router.get("/cloud-browser/screen", response_model=None)
def cloud_browser_screen(user: User = Depends(get_current_user)) -> Response:
    _cloud_on()
    jpeg = cloud_browser.pool().frame(user.id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="no_screen")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"cache-control": "no-store", "x-screen-version": str(cloud_browser.pool().frame_version(user.id))},
    )


class CloudInputBody(BaseModel):
    kind: str = Field(pattern="^(click|text|key|scroll|open|back|refresh)$")
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    text: str | None = Field(default=None, max_length=2000)
    key: str | None = Field(default=None, max_length=32)
    dy: float | None = Field(default=None, ge=-3000, le=3000)
    url: str | None = Field(default=None, max_length=2000)


@router.post("/cloud-browser/input")
def cloud_browser_input(body: CloudInputBody, user: User = Depends(get_current_user)) -> dict:
    """The user's own click or keystroke on the live screen: sign in to a service, pass a captcha."""
    _cloud_on()
    result = cloud_browser.pool().user_input(user.id, body.model_dump(exclude_none=True))
    if result.error:
        raise HTTPException(status_code=_CLOUD_STATUS.get(result.error, 502), detail=result.error)
    return cloud_browser.pool().status(user.id)


@router.post("/cloud-browser/stop")
def cloud_browser_stop(user: User = Depends(get_current_user)) -> dict:
    _cloud_on()
    cloud_browser.pool().stop(user.id)
    return cloud_browser.pool().status(user.id)


@router.post("/cloud-browser/forget")
def cloud_browser_forget(user: User = Depends(get_current_user)) -> dict:
    """Sign out of every site in the cloud browser and delete the saved sign-ins."""
    _cloud_on()
    cloud_browser.pool().forget(user.id)
    return cloud_browser.pool().status(user.id)


# --- files between the cloud computer and the user's PC ---------------------------------

from urllib.parse import unquote  # noqa: E402

from app import files as heli_files  # noqa: E402

_FILE_STATUS = {"not_found": 404, "file_too_large": 413, "storage_full": 507, "bad_direction": 400}


def _file_fail(exc: heli_files.FileError) -> None:
    raise HTTPException(status_code=_FILE_STATUS.get(exc.code, 400), detail=exc.code) from None


@router.post("/files", status_code=201)
async def files_upload(
    request: Request,
    x_file_name: str = Header(default="", max_length=1024),
    user: User = Depends(get_current_user),
) -> dict:
    """The user attaches a file in the chat. Raw body; the name comes URL-encoded in X-File-Name."""
    limit = heli_files.store().max_bytes
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        raise HTTPException(status_code=413, detail="file_too_large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="file_too_large")
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="empty_file")
    try:
        info = heli_files.store().put_bytes(user.id, unquote(x_file_name), b"".join(chunks), heli_files.FROM_PC)
    except heli_files.FileError as exc:
        _file_fail(exc)
    return info.public()


@router.get("/files")
def files_list(
    direction: str | None = None,
    pending: bool = False,
    user: User = Depends(get_current_user),
) -> dict:
    """pending=true: files for the PC that the app has not saved yet."""
    items = heli_files.store().list(user.id, direction)
    if pending:
        items = [item for item in items if item.direction == heli_files.TO_PC and not item.delivered]
    return {"files": [item.public() for item in items], "max_bytes": heli_files.store().max_bytes}


@router.get("/files/{file_id}/content", response_model=None)
def files_content(file_id: str, user: User = Depends(get_current_user)) -> Response:
    try:
        info, blob = heli_files.store().get(user.id, file_id)
    except heli_files.FileError as exc:
        _file_fail(exc)
    from urllib.parse import quote as _quote

    return Response(
        content=blob.read_bytes(),
        media_type="application/octet-stream",
        headers={
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "content-disposition": f"attachment; filename*=UTF-8''{_quote(info.name)}",
            "x-file-name": _quote(info.name),
        },
    )


@router.post("/files/{file_id}/delivered")
def files_delivered(file_id: str, user: User = Depends(get_current_user)) -> dict:
    try:
        return heli_files.store().mark_delivered(user.id, file_id).public()
    except heli_files.FileError as exc:
        _file_fail(exc)


@router.delete("/files/{file_id}")
def files_delete(file_id: str, user: User = Depends(get_current_user)) -> dict:
    try:
        heli_files.store().delete(user.id, file_id)
    except heli_files.FileError as exc:
        _file_fail(exc)
    return {"deleted": True}
