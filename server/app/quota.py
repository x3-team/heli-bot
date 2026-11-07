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

from app import billing
from app.config import get_settings
from app.llm.client import Completion, HeliClient
from app.llm.prices import cost_rub
from app.models import QuotaAlert, UsageLedger, User
from app.notify import notify

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
    global_daily_used: Decimal
    global_daily_limit: Decimal
    blocked: bool
    warning: bool
    week_key: str
    month_key: str
    # Plan accounts (app/billing.py): the month is the paid period, overage may extend it.
    month_start: datetime | None = None
    period_ends_at: datetime | None = None
    plan_expired: bool = False
    overage_budget: Decimal = Decimal("0")

    @property
    def day_percent(self) -> Decimal:
        return _percent(self.daily_used, self.daily_limit) if self.daily_limit > 0 else Decimal("0")

    def over_limit(self, extra: Decimal, *, already: bool = False) -> Decimal:
        """Part of `extra` cost above the plan's day or month limit (the larger one: one cost, paid once).

        already=True: `extra` is in month_used/daily_used already (a charge just written)."""
        month = self.month_used - (extra if already else Decimal("0"))
        day = self.daily_used - (extra if already else Decimal("0"))
        over_month = month + extra - max(self.monthly_limit, month)
        over_day = (day + extra - max(self.daily_limit, day)) if self.daily_limit > 0 else Decimal("0")
        return max(Decimal("0"), over_month, over_day)

    def overage_needed(self, extra: Decimal = Decimal("0")) -> Decimal:
        """Wallet rubles (with markup) that `extra` more cost needs above the plan limits."""
        return self.over_limit(extra) * billing.OVERAGE_MARKUP


def weekly_limit_of(user: User) -> Decimal:
    if billing.plan_of(user) is not None:
        # Plans have day + month limits only: the week never binds before the month.
        return monthly_limit_of(user)
    if user.weekly_limit_rub is not None:
        return _money(user.weekly_limit_rub)
    monthly = _money(user.monthly_limit_rub)
    if monthly <= 0:
        monthly = _money(get_settings().default_monthly_limit_rub)
    return (monthly / Decimal(4) * Decimal("1.25")).quantize(Decimal("0.000001"))


def monthly_limit_of(user: User) -> Decimal:
    plan = billing.plan_of(user)
    if plan is not None:
        return _money(plan.month_limit_rub)
    monthly = _money(user.monthly_limit_rub)
    if monthly <= 0:
        return _money(get_settings().default_monthly_limit_rub)
    return monthly


def snapshot(session: Session, user: User, now: datetime | None = None) -> UsageSnapshot:
    week_local, month_local, day_local = period_bounds(now)
    week_start = week_local.astimezone(timezone.utc)
    month_start = month_local.astimezone(timezone.utc)
    day_start = day_local.astimezone(timezone.utc)
    plan = billing.plan_of(user)
    month_key = month_local.strftime("%Y-%m")
    if plan is not None and user.plan_started_at is not None:
        month_start = billing.aware(user.plan_started_at)
        month_key = month_start.astimezone(MSK).strftime("%Y-%m-%dT%H:%M")
    week_used = _spent(session, UsageLedger.user_id == user.id, UsageLedger.created_at >= week_start)
    month_used = _spent(session, UsageLedger.user_id == user.id, UsageLedger.created_at >= month_start)
    # Every limit is per account: a training pack and a tester never share a day.
    daily_used = _spent(session, UsageLedger.user_id == user.id, UsageLedger.created_at >= day_start)
    weekly_limit = weekly_limit_of(user)
    monthly_limit = monthly_limit_of(user)
    settings = get_settings()
    daily_limit = _money(plan.day_limit_rub) if plan is not None else _money(settings.daily_limit_rub)
    # Server-wide day cap only when GLOBAL_DAILY_LIMIT_RUB > 0 (off by default).
    global_daily_limit = _money(settings.global_daily_limit_rub)
    global_daily_used = (
        _spent(session, UsageLedger.created_at >= day_start) if global_daily_limit > 0 else Decimal("0")
    )
    week_percent = _percent(week_used, weekly_limit)
    month_percent = _percent(month_used, monthly_limit)
    plan_expired = plan is not None and billing.plan_status(user, now) in ("expired", "unpaid")
    overage = billing.overage_budget(session, user, month_start) if plan is not None else Decimal("0")
    day_out = daily_limit > 0 and daily_used >= daily_limit
    if plan is not None:
        # Over the day or month limit a plan goes on with overage from the wallet, if the client allowed it.
        limits_out = (month_used >= monthly_limit or day_out) and overage <= 0
    else:
        limits_out = week_percent >= 100 or month_percent >= 100 or day_out
    blocked = (
        plan_expired
        or limits_out
        or (global_daily_limit > 0 and global_daily_used >= global_daily_limit)
    )
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
        global_daily_used=global_daily_used,
        global_daily_limit=global_daily_limit,
        blocked=blocked,
        warning=warning,
        week_key=week_local.date().isoformat(),
        month_key=month_key,
        month_start=month_start,
        period_ends_at=billing.aware(user.plan_ends_at) if plan is not None and user.plan_ends_at else None,
        plan_expired=plan_expired,
        overage_budget=overage,
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


# A downscaled screenshot costs about this many input tokens on vision models.
IMAGE_TOKENS = 1600


def estimate_input_tokens(messages: list[dict]) -> int:
    chars = 0
    images = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    images += 1
                elif isinstance(part, dict):
                    chars += len(str(part.get("text", "")))
        else:
            chars += len(str(content))
        for call in message.get("tool_calls") or []:
            chars += len(str(call.get("function", {}).get("arguments", "")))
    return max(1, chars // 4 + images * IMAGE_TOKENS)


def estimate_turn_reserve(request: str) -> Decimal:
    """Worst-case reserve for one classify + answer pass (matches run_turn metering)."""
    from app.llm.router import classification_messages

    classify = cost_rub(
        "heli-fast",
        estimate_input_tokens(classification_messages(request)),
        80,
    )
    # A typical turn with internet access: a few tool rounds that re-send the
    # growing context, then the answer.
    answer = cost_rub(
        "heli-fast",
        estimate_input_tokens([{"role": "user", "content": request}]) + 8000,
        1500,
    ) * 3
    return classify + answer


def metered_complete(
    session: Session,
    user: User,
    task_id: uuid.UUID | None,
    client: HeliClient,
    alias: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
) -> Completion:
    """Reserve the worst-case cost, then replace it with the real charge."""
    if alias.startswith("heli-smart") and not billing.smart_allowed(user):
        # The smart model is a «Про» feature; other plans get the everyday one.
        alias = "heli-fast"
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
        if tools:
            completion = client.complete(alias, messages, max_tokens=max_tokens, tools=tools)
        else:
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
    settle_overage(session, user, hold)
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
    settle_overage(session, user, row, at)
    record_alerts(session, user, at)
    return row


def settle_overage(session: Session, user: User, row: UsageLedger, now: datetime | None = None) -> None:
    """The part of this charge above the plan's month limit is paid from the wallet (× markup)."""
    if billing.plan_of(user) is None or not user.overage_enabled:
        return
    snap = snapshot(session, user, now)
    over = snap.over_limit(_money(row.amount_rub), already=True)
    if over <= 0:
        return
    budget = billing.overage_budget(session, user, snap.month_start or _aware(now))
    debit = min(billing.rub(over * billing.OVERAGE_MARKUP), budget)
    if debit <= 0:
        return
    billing.post_tx(
        session,
        user,
        "overage",
        -debit,
        "Сверх лимита тарифа",
        ref=f"overage:{row.id}",
        allow_partial=True,
        now=now,
    )


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
            label = "недели" if window == "week" else "месяца"
            notify(f"Лимит {label} достиг {threshold}%.")
    session.flush()


def period_bounds(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    local = _aware(now).astimezone(MSK)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return week, month, day


def _ensure_room(session: Session, user: User, reserve: Decimal) -> None:
    billing.refresh(session, user)
    snap = snapshot(session, user)
    if snap.plan_expired:
        raise QuotaError("plan_required")
    if snap.blocked:
        raise QuotaError("limit_reached")
    if billing.plan_of(user) is None and snap.week_used + reserve > snap.weekly_limit:
        raise QuotaError("limit_reached")
    if billing.plan_of(user) is not None:
        if snap.overage_needed(reserve) > snap.overage_budget:
            raise QuotaError("limit_reached")
    else:
        if snap.month_used + reserve > snap.monthly_limit:
            raise QuotaError("limit_reached")
        if snap.daily_limit > 0 and snap.daily_used + reserve > snap.daily_limit:
            raise QuotaError("limit_reached")
    if snap.global_daily_limit > 0 and snap.global_daily_used + reserve > snap.global_daily_limit:
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
