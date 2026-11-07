/** Heli bot's own wallet and plans (server: app/billing.py, API /v1/billing*). Only percents of the
 * plan limit are shown; rubles appear only for plan prices, the wallet and spending over the limit. */

/** unpaid: a new account that has not bought a plan yet (access is paid from the start, no trial). */
export type PlanStatus = "none" | "unpaid" | "trial" | "active" | "expired";

export interface PlanOffer {
  id: string;
  title: string;
  priceRub: number;
  days: number;
  /** First line says who the plan is for; the rest are the plan's features. */
  features: string[];
}

export interface Billing {
  plan: {
    id: string | null;
    title: string;
    priceRub: number | null;
    status: PlanStatus;
    periodEndsAt: string | null;
    autoRenew: boolean;
  };
  walletRub: number;
  usage: {
    dayPercent: number;
    monthPercent: number;
    blocked: boolean;
    warning: boolean;
    inOverage: boolean;
    periodEndsAt: string | null;
    tasksLeftMonth: number | null;
    tasksLeftToday: number | null;
  };
  overage: { enabled: boolean; capRub: number; spentRub: number; typicalTaskRub: number | null };
  plans: PlanOffer[];
  topup: { available: boolean; minRub: number };
}

export interface WalletOperation {
  id: string;
  kind: string;
  title: string;
  amountRub: number;
  createdAt: string;
}

export class BillingError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const BILLING_ERRORS: Record<string, string> = {
  insufficient_funds: "На кошельке не хватает денег. Пополните кошелёк и попробуйте снова.",
  plan_required: "Чтобы ставить задачи, нужен тариф. Выберите или продлите его в разделе «Тариф».",
  bad_plan: "Такого тарифа нет.",
  payments_not_configured: "Оплата картой пока не подключена. Пополним по счёту через менеджера.",
};

const num = (value: unknown, fallback = 0): number => {
  const n = typeof value === "number" ? value : Number.parseFloat(String(value ?? ""));
  return Number.isFinite(n) ? n : fallback;
};

const optNum = (value: unknown): number | null => (value === null || value === undefined || value === "" ? null : num(value));

const STATUSES: PlanStatus[] = ["none", "unpaid", "trial", "active", "expired"];

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function parseBilling(body: any): Billing {
  const plan = body?.plan ?? {};
  const usage = body?.usage ?? {};
  const overage = body?.overage ?? {};
  const status = STATUSES.includes(plan.status) ? (plan.status as PlanStatus) : "none";
  return {
    plan: {
      id: plan.id ?? null,
      title: String(plan.title ?? "Бета-тест"),
      priceRub: optNum(plan.price_rub),
      status,
      periodEndsAt: plan.period_ends_at ?? null,
      autoRenew: Boolean(plan.auto_renew),
    },
    walletRub: num(body?.wallet?.balance_rub),
    usage: {
      dayPercent: num(usage.day_percent),
      monthPercent: num(usage.month_percent),
      blocked: Boolean(usage.blocked),
      warning: Boolean(usage.warning),
      inOverage: Boolean(usage.in_overage),
      periodEndsAt: usage.period_ends_at ?? null,
      tasksLeftMonth: optNum(usage.tasks_left_month),
      tasksLeftToday: optNum(usage.tasks_left_today),
    },
    overage: {
      enabled: Boolean(overage.enabled),
      capRub: num(overage.cap_rub),
      spentRub: num(overage.spent_rub),
      typicalTaskRub: optNum(overage.typical_task_rub),
    },
    plans: Array.isArray(body?.plans)
      ? // eslint-disable-next-line @typescript-eslint/no-explicit-any
        body.plans.map((item: any) => ({
          id: String(item.id),
          title: String(item.title),
          priceRub: num(item.price_rub),
          days: num(item.days, 30),
          features: Array.isArray(item.features) ? item.features.map(String) : [],
        }))
      : [],
    topup: { available: Boolean(body?.topup?.available), minRub: num(body?.topup?.min_rub, 100) },
  };
}

/** No working plan: never bought (unpaid) or ran out (expired). The server answers tasks with 402. */
export function needsPlan(billing: Billing | null): boolean {
  return billing !== null && (billing.plan.status === "unpaid" || billing.plan.status === "expired");
}

/** How much to put on the wallet before this price can be paid (0 when the wallet already covers it). */
export function topupShort(billing: Billing, priceRub: number): number {
  const short = Math.ceil(priceRub - billing.walletRub);
  return short <= 0 ? 0 : Math.max(billing.topup.minRub, short);
}

/** 1490 → «1 490 ₽», 64.5 → «64,50 ₽». */
export function rub(value: number): string {
  const whole = Math.abs(value - Math.round(value)) < 0.005;
  const text = whole
    ? Math.round(value).toLocaleString("ru-RU")
    : value.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${text.replace(/ /g, " ")} ₽`;
}

/** «1 декабря»; with time: «7 нояб, 16:40». */
export function dayLabel(iso: string | null, withTime = false): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  if (!withTime) return date.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
  const day = date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }).replace(".", "");
  const time = date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return `${day}, ${time}`;
}

/** «ещё ~N задач», or nothing when the server does not know. */
export function tasksLeftLabel(count: number | null, when: string): string {
  if (count === null) return "";
  if (count <= 0) return `задачи ${when} закончились`;
  return `ещё ~${count.toLocaleString("ru-RU").replace(/ /g, " ")} ${plural(count, "задача", "задачи", "задач")} ${when}`;
}

function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

async function call(base: string, token: string, path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { authorization: `Bearer ${token}`, ...(init.body ? { "content-type": "application/json" } : {}), ...init.headers },
  });
  if (!response.ok) {
    let code = "";
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") code = body.detail;
    } catch {
      // not JSON
    }
    const message =
      BILLING_ERRORS[code] ??
      (response.status === 401 ? "Нужно войти снова." : response.status >= 500 ? "Сервер недоступен. Попробуйте позже." : `Ошибка ${response.status}`);
    throw new BillingError(message, response.status, code);
  }
  return response;
}

/** null when the server has no wallet yet (404): the cabinet then shows the old percent limits. */
export async function loadBilling(base: string, token: string): Promise<Billing | null> {
  try {
    return parseBilling(await (await call(base, token, "/v1/billing")).json());
  } catch (err) {
    if (err instanceof BillingError && err.status === 404) return null;
    throw err;
  }
}

export async function loadWalletHistory(base: string, token: string): Promise<WalletOperation[]> {
  const body = (await (await call(base, token, "/v1/billing/history?limit=50")).json()) as { items?: unknown[] };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (body.items ?? []).map((item: any) => ({
    id: String(item.id),
    kind: String(item.kind),
    title: String(item.title),
    amountRub: num(item.amount_rub),
    createdAt: String(item.created_at),
  }));
}

export async function choosePlan(base: string, token: string, plan: string): Promise<Billing> {
  return parseBilling(await (await call(base, token, "/v1/billing/plan", { method: "POST", body: JSON.stringify({ plan }) })).json());
}

export async function saveBillingSettings(
  base: string,
  token: string,
  settings: { autoRenew?: boolean; overageEnabled?: boolean; overageCapRub?: number },
): Promise<Billing> {
  const body: Record<string, unknown> = {};
  if (settings.autoRenew !== undefined) body.auto_renew = settings.autoRenew;
  if (settings.overageEnabled !== undefined) body.overage_enabled = settings.overageEnabled;
  if (settings.overageCapRub !== undefined) body.overage_cap_rub = settings.overageCapRub;
  return parseBilling(await (await call(base, token, "/v1/billing/settings", { method: "PUT", body: JSON.stringify(body) })).json());
}

/** Card payment page when acquiring is connected; BillingError 501 payments_not_configured until then. */
export async function requestTopup(base: string, token: string, amountRub: number): Promise<string | null> {
  const body = (await (await call(base, token, "/v1/billing/topup", { method: "POST", body: JSON.stringify({ amount_rub: amountRub }) })).json()) as {
    confirmation_url?: string;
  };
  return body.confirmation_url ?? null;
}
