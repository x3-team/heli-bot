"""Quota ledger. Callers see percents. Ruble amounts stay on the server."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import Completion, HeliClient
from app.llm.prices import cost_rub
from app.models import QuotaAlert, UsageLedger, User

log = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
ALERTS = (50, 80, 95)
RESET_LABEL = "пн 00:00 МСК"


class QuotaError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class UsageSnapshot:
    week_percent: Decimal
    month_percent: Decimal
    week_used: Decimal
    month_used: Decimal
    daily_used: Decimal
    weekly_limit: Decimal
    monthly_limit: Decimal
    daily_limit: Decimal
    blocked: bool
    warning: bool
    week_key: str
    month_key: str


def weekly_limit_of(user: User) -> Decimal:
    if user.weekly_limit_rub is not None:
        return _money(user.weekly_limit_rub)
    monthly = _money(user.monthly_limit_rub)
    if monthly <= 0:
        monthly = _money(get_settings().default_monthly_limit_rub)
    return (monthly / Decimal(4) * Decimal("1.25")).quantize(Decimal("0.000001"))


def monthly_limit_of(user: User) -> Decimal:
    monthly = _money(user.monthly_limit_rub)
    if monthly <= 0:
        return _money(get_settings().default_monthly_limit_rub)
    return monthly


def snapshot(session: Session, user: User, now: datetime | None = None) -> UsageSnapshot:
    week_local, month_local, day_local = period_bounds(now)
    week_start = week_local.astimezone(timezone.utc)
    month_start = month_local.astimezone(timezone.utc)
    day_start = day_local.astimezone(timezone.utc)
    week_used = _spent(session, UsageLedger.user_id == user.id, UsageLedger.created_at >= week_start)
    month_used = _spent(session, UsageLedger.user_id == user.id, UsageLedger.created_at >= month_start)
    daily_used = _spent(session, UsageLedger.created_at >= day_start)
    weekly_limit = weekly_limit_of(user)
    monthly_limit = monthly_limit_of(user)
    daily_limit = _money(get_settings().daily_limit_rub)
    week_percent = _percent(week_used, weekly_limit)
    month_percent = _percent(month_used, monthly_limit)
    blocked = week_percent >= 100 or month_percent >= 100 or (daily_limit <= 0 or daily_used >= daily_limit)
    warning = week_percent >= 80 or month_percent >= 80
    return UsageSnapshot(
        week_percent=week_percent,
        month_percent=month_percent,
        week_used=week_used,
        month_used=month_used,
        daily_used=daily_used,
        weekly_limit=weekly_limit,
        monthly_limit=monthly_limit,
        daily_limit=daily_limit,
        blocked=blocked,
        warning=warning,
        week_key=week_local.date().isoformat(),
        month_key=month_local.strftime("%Y-%m"),
    )


def public_usage(snap: UsageSnapshot, *, trial_active: bool = False) -> dict:
    return {
        "week_percent": _public_number(snap.week_percent),
        "month_percent": _public_number(snap.month_percent),
        "blocked": snap.blocked,
        "warning": snap.warning,
        "reset_label": RESET_LABEL,
        "trial_active": trial_active,
    }


def remaining_ratio(snap: UsageSnapshot) -> Decimal:
    week_left = _ratio_left(snap.week_used, snap.weekly_limit)
    month_left = _ratio_left(snap.month_used, snap.monthly_limit)
    return min(week_left, month_left)


def estimate_input_tokens(messages: list[dict]) -> int:
    chars = 0
    for message in messages:
        chars += len(str(message.get("content", "")))
    return max(1, chars // 4)


def metered_complete(
    session: Session,
    user: User,
    task_id: uuid.UUID | None,
    client: HeliClient,
    alias: str,
    messages: list[dict],
    max_tokens: int,
) -> Completion:
    """Reserve the worst-case cost, then replace it with the real charge."""
    input_estimate = estimate_input_tokens(messages)
    reserve = cost_rub(alias, input_estimate, max_tokens)
    _ensure_room(session, user, reserve)
    hold = UsageLedger(
        user_id=user.id,
        task_id=task_id,
        alias=alias,
        input_tokens=input_estimate,
        output_tokens=max_tokens,
        amount_rub=reserve,
        reserved=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(hold)
    session.flush()
    try:
        completion = client.complete(alias, messages, max_tokens=max_tokens)
    except Exception:
        session.delete(hold)
        session.flush()
        raise
    hold.input_tokens = completion.input_tokens
    hold.output_tokens = completion.output_tokens
    hold.amount_rub = completion.cost_rub
    hold.reserved = False
    session.flush()
    record_alerts(session, user)
    return completion


def record_charge(
    session: Session,
    user: User,
    amount: Decimal,
    *,
    alias: str = "heli-fast",
    task_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> UsageLedger:
    row = UsageLedger(
        user_id=user.id,
        task_id=task_id,
        alias=alias,
        input_tokens=0,
        output_tokens=0,
        amount_rub=_money(amount),
        reserved=False,
        created_at=_aware(at),
    )
    session.add(row)
    session.flush()
    record_alerts(session, user, at)
    return row


def record_alerts(session: Session, user: User, now: datetime | None = None) -> None:
    snap = snapshot(session, user, now)
    windows = (("week", snap.week_percent, snap.week_key), ("month", snap.month_percent, snap.month_key))
    for window, percent, period_key in windows:
        for threshold in ALERTS:
            if percent < threshold:
                continue
            exists = session.scalar(
                select(QuotaAlert.id).where(
                    QuotaAlert.user_id == user.id,
                    QuotaAlert.window == window,
                    QuotaAlert.threshold == threshold,
                    QuotaAlert.period_key == period_key,
                )
            )
            if exists is not None:
                continue
            session.add(
                QuotaAlert(
                    user_id=user.id,
                    window=window,
                    threshold=threshold,
                    period_key=period_key,
                    created_at=datetime.now(timezone.utc),
                )
            )
            log.info("quota alert window=%s threshold=%s", window, threshold)
    session.flush()


def period_bounds(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    local = _aware(now).astimezone(MSK)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return week, month, day


def _ensure_room(session: Session, user: User, reserve: Decimal) -> None:
    snap = snapshot(session, user)
    if snap.blocked:
        raise QuotaError("limit_reached")
    if snap.week_used + reserve > snap.weekly_limit:
        raise QuotaError("limit_reached")
    if snap.month_used + reserve > snap.monthly_limit:
        raise QuotaError("limit_reached")
    if snap.daily_used + reserve > snap.daily_limit:
        raise QuotaError("limit_reached")


def _spent(session: Session, *filters) -> Decimal:
    value = session.scalar(select(func.coalesce(func.sum(UsageLedger.amount_rub), 0)).where(*filters))
    return _money(value)


def _percent(used: Decimal, limit: Decimal) -> Decimal:
    if limit <= 0:
        return Decimal("100")
    return (used / limit * Decimal(100)).quantize(Decimal("0.01"))


def _public_number(value: Decimal) -> float:
    capped = min(Decimal("100"), value)
    return float(capped.quantize(Decimal("0.01")))


def _ratio_left(used: Decimal, limit: Decimal) -> Decimal:
    if limit <= 0:
        return Decimal("0")
    left = (limit - used) / limit
    if left < 0:
        return Decimal("0")
    return left


def _money(value) -> Decimal:
    if value is None:
        return Decimal("0.000000")
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now
