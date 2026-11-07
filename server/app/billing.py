"""Heli bot's own wallet and plans (bot.getheli.ru). Separate from the getheli.ru API balance.

The wallet holds the client's rubles. A plan is paid from it for a period; inside the period the
agent spends within the plan's cost limits (quota.py shows only percents). Past the month limit the
client may opt in to overage: real cost × OVERAGE_MARKUP, debited from the wallet up to a monthly cap.
The wallet never goes below zero.

Accounts with no plan (beta testers, training accounts) keep their old limits untouched."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, WalletTx

log = logging.getLogger(__name__)

OVERAGE_MARKUP = Decimal("1.5")
KOPECK = Decimal("0.01")
TX_KINDS = ("topup", "plan", "refund", "overage", "adjust")


class BillingError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    price_rub: Decimal
    # Model cost limits, rubles. Never shown to the client: only percents leave the server.
    month_limit_rub: Decimal
    day_limit_rub: Decimal
    days: int
    schedules: int
    # Per task: step limit, one-time extension when it is still moving, cost ceiling.
    max_steps: int
    extend_steps: int
    task_max_rub: Decimal
    # heli-smart / heli-smart-alt for planning and after two errors in a row.
    smart: bool
    features: tuple[str, ...]
    public: bool = True


PLANS: dict[str, Plan] = {
    # A paid-only sign-up before the first purchase: no limits, every task asks for a plan.
    "unpaid": Plan(
        id="unpaid",
        title="Тариф не выбран",
        price_rub=Decimal("0"),
        month_limit_rub=Decimal("0"),
        day_limit_rub=Decimal("0"),
        days=0,
        schedules=0,
        max_steps=0,
        extend_steps=0,
        task_max_rub=Decimal("0"),
        smart=False,
        features=(),
        public=False,
    ),
    "trial": Plan(
        id="trial",
        title="Пробная неделя",
        price_rub=Decimal("0"),
        month_limit_rub=Decimal("30"),
        day_limit_rub=Decimal("15"),
        days=7,
        schedules=1,
        max_steps=30,
        extend_steps=0,
        task_max_rub=Decimal("10"),
        smart=False,
        features=("7 дней бесплатно", "Возможности тарифа «Старт»"),
        public=False,
    ),
    "start": Plan(
        id="start",
        title="Старт",
        price_rub=Decimal("390"),
        month_limit_rub=Decimal("120"),
        day_limit_rub=Decimal("15"),
        days=30,
        schedules=1,
        max_steps=30,
        extend_steps=0,
        task_max_rub=Decimal("10"),
        smart=False,
        features=(
            "Попробовать, личные задачи, справки",
            "~350 лёгких, или ~75 средних, или ~16 длинных задач в месяц",
            "До 30 шагов на задачу",
            "1 расписание",
            "Интеграции: чтение",
            "Файлы и память",
        ),
    ),
    "business": Plan(
        id="business",
        title="Бизнес",
        price_rub=Decimal("1490"),
        month_limit_rub=Decimal("500"),
        day_limit_rub=Decimal("50"),
        days=30,
        schedules=10,
        max_steps=40,
        extend_steps=20,
        task_max_rub=Decimal("30"),
        smart=False,
        features=(
            "Ежедневная работа в 1С, МоёмСкладе, CRM",
            "~1 500 лёгких, или ~300 средних, или ~70 длинных задач в месяц",
            "До 40 шагов на задачу, +20 один раз",
            "10 расписаний",
            "Интеграции: чтение и запись с подтверждением",
            "Файлы и память",
        ),
    ),
    "pro": Plan(
        id="pro",
        title="Про",
        price_rub=Decimal("3990"),
        month_limit_rub=Decimal("1400"),
        day_limit_rub=Decimal("150"),
        days=30,
        schedules=30,
        max_steps=40,
        extend_steps=20,
        task_max_rub=Decimal("30"),
        smart=True,
        features=(
            "Бухгалтер, снабженец, владелец с длинными задачами",
            "~4 000 лёгких, или ~850 средних, или ~190 длинных задач в месяц",
            "До 40 шагов на задачу, +20 один раз",
            "Умная модель для плана и сложных мест",
            "30 расписаний",
            "Интеграции: чтение и запись с подтверждением",
            "Файлы и память",
        ),
    ),
}


def rub(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(KOPECK, rounding=ROUND_HALF_UP)


def aware(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def plan_of(user: User) -> Plan | None:
    return PLANS.get(user.plan or "")


def plan_status(user: User, now: datetime | None = None) -> str:
    """none (no plan: beta/training account, old limits) | unpaid (signed up, nothing bought) | trial | active | expired."""
    plan = plan_of(user)
    if plan is None:
        return "none"
    if plan.id == "unpaid":
        return "unpaid"
    if user.plan_ends_at is None or aware(now) >= aware(user.plan_ends_at):
        return "expired"
    return "trial" if plan.id == "trial" else "active"


def balance(user: User) -> Decimal:
    return rub(user.wallet_rub)


def post_tx(
    session: Session,
    user: User,
    kind: str,
    amount,
    title: str,
    *,
    ref: str | None = None,
    allow_partial: bool = False,
    now: datetime | None = None,
) -> WalletTx | None:
    """One wallet movement. `ref` makes it idempotent (a payment id, an overage ledger row).

    A debit larger than the balance fails with insufficient_funds, or with allow_partial takes
    what is there: the wallet never goes below zero."""
    if kind not in TX_KINDS:
        raise BillingError("bad_kind")
    if ref is not None:
        existing = session.scalar(select(WalletTx).where(WalletTx.ref == ref))
        if existing is not None:
            return existing
    amount = rub(amount)
    current = balance(user)
    if current + amount < 0:
        if not allow_partial:
            raise BillingError("insufficient_funds")
        amount = -current
    if amount == 0:
        return None
    user.wallet_rub = current + amount
    row = WalletTx(
        user_id=user.id,
        kind=kind,
        amount_rub=amount,
        balance_rub=user.wallet_rub,
        title=title[:200],
        ref=ref,
        created_at=aware(now),
    )
    session.add(row)
    session.flush()
    log.info("wallet tx kind=%s", kind)
    return row


def start_trial(user: User, now: datetime | None = None) -> None:
    """New sign-ups with HELI_BILLING on: the trial week if HELI_TRIAL is on, else nothing until they buy a plan.

    HELI_BILLING off: old behaviour, no plan."""
    settings = get_settings()
    if not settings.billing_enabled:
        return
    moment = aware(now)
    if not settings.trial_enabled:
        user.plan = "unpaid"
        user.plan_started_at = None
        user.plan_ends_at = None
        user.trial_ends_at = None  # no «Пробная неделя» badge in the apps
        return
    user.plan = "trial"
    user.plan_started_at = moment
    user.plan_ends_at = moment + timedelta(days=PLANS["trial"].days)


def _unused_part(user: User, plan: Plan, now: datetime) -> Decimal:
    if plan.price_rub <= 0 or user.plan_started_at is None or user.plan_ends_at is None:
        return Decimal("0")
    start, end = aware(user.plan_started_at), aware(user.plan_ends_at)
    if now >= end or end <= start:
        return Decimal("0")
    left = Decimal((end - now).total_seconds()) / Decimal((end - start).total_seconds())
    return rub(plan.price_rub * min(Decimal(1), left))


def buy_plan(session: Session, user: User, plan_id: str, now: datetime | None = None) -> None:
    """Buy or switch a plan from the wallet. The unused share of a paid current plan comes back first."""
    plan = PLANS.get(plan_id)
    if plan is None or not plan.public:
        raise BillingError("bad_plan")
    moment = aware(now)
    current = plan_of(user)
    refund = Decimal("0")
    if current is not None and plan_status(user, moment) == "active":
        refund = _unused_part(user, current, moment)
    if balance(user) + refund < plan.price_rub:
        raise BillingError("insufficient_funds")
    stamp = moment.isoformat()
    if refund > 0:
        post_tx(session, user, "refund", refund, f"Остаток тарифа «{current.title}»", ref=f"refund:{user.id}:{stamp}", now=moment)
    post_tx(session, user, "plan", -plan.price_rub, f"Тариф «{plan.title}» на {plan.days} дней", ref=f"plan:{user.id}:{stamp}", now=moment)
    user.plan = plan.id
    user.plan_started_at = moment
    user.plan_ends_at = moment + timedelta(days=plan.days)
    session.flush()
    log.info("plan bought plan=%s", plan.id)


def refresh(session: Session, user: User, now: datetime | None = None) -> None:
    """Renew an ended paid plan from the wallet when auto-renew is on and there is money."""
    plan = plan_of(user)
    moment = aware(now)
    if plan is None or plan_status(user, moment) != "expired":
        return
    if plan.price_rub <= 0 or not user.plan_auto_renew or balance(user) < plan.price_rub:
        return
    ended = aware(user.plan_ends_at) if user.plan_ends_at else moment
    post_tx(
        session,
        user,
        "plan",
        -plan.price_rub,
        f"Продление тарифа «{plan.title}» на {plan.days} дней",
        ref=f"renew:{user.id}:{ended.isoformat()}",
        now=moment,
    )
    user.plan_started_at = moment
    user.plan_ends_at = moment + timedelta(days=plan.days)
    session.flush()
    log.info("plan renewed plan=%s", plan.id)


def overage_spent(session: Session, user: User, since: datetime) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(WalletTx.amount_rub), 0)).where(
            WalletTx.user_id == user.id,
            WalletTx.kind == "overage",
            WalletTx.created_at >= since,
        )
    )
    return -rub(value)


def overage_budget(session: Session, user: User, since: datetime) -> Decimal:
    """Rubles the wallet may still give to overage this period (already with the markup)."""
    if plan_of(user) is None or not user.overage_enabled:
        return Decimal("0")
    cap_left = rub(user.overage_cap_rub) - overage_spent(session, user, since)
    return max(Decimal("0"), min(balance(user), cap_left))


# Typical mix (60% light, 30% medium, 10% long) costs about this much per task: heli-pricing v2.
TYPICAL_TASK_RUB = Decimal("1.4")


def tasks_left(cost_left: Decimal) -> int:
    """Approximate number of typical tasks the remaining limit covers (shown instead of rubles)."""
    if cost_left <= 0:
        return 0
    return int(cost_left / TYPICAL_TASK_RUB)


def smart_allowed(user: User) -> bool:
    plan = plan_of(user)
    return plan is None or plan.smart


def task_caps(user: User) -> tuple[int | None, int | None, Decimal | None]:
    """(max_steps, extend_steps, max_rub) of the plan; None = keep the server default."""
    plan = plan_of(user)
    if plan is None:
        return None, None, None
    return plan.max_steps, plan.extend_steps, plan.task_max_rub


def schedule_limit(user: User) -> int | None:
    plan = plan_of(user)
    return plan.schedules if plan is not None else None


def history(session: Session, user: User, limit: int = 50) -> list[dict]:
    rows = session.scalars(
        select(WalletTx)
        .where(WalletTx.user_id == user.id)
        .order_by(WalletTx.created_at.desc(), WalletTx.id)
        .limit(max(1, min(limit, 200)))
    )
    return [
        {
            "id": str(row.id),
            "kind": row.kind,
            "title": row.title,
            "amount_rub": str(rub(row.amount_rub)),
            "balance_rub": str(rub(row.balance_rub)),
            "created_at": aware(row.created_at).isoformat(),
        }
        for row in rows
    ]


def public_plans() -> list[dict]:
    return [
        {
            "id": plan.id,
            "title": plan.title,
            "price_rub": str(rub(plan.price_rub)),
            "days": plan.days,
            "features": list(plan.features),
            "limits": {
                "max_steps": plan.max_steps,
                "extend_steps": plan.extend_steps,
                "schedules": plan.schedules,
                "smart_model": plan.smart,
                "integrations_write": plan.id in ("business", "pro"),
                "tasks_per_month": int(plan.month_limit_rub / TYPICAL_TASK_RUB),
            },
        }
        for plan in PLANS.values()
        if plan.public
    ]


def new_ref() -> str:
    return f"manual:{uuid.uuid4()}"
