import assert from "node:assert/strict";
import test from "node:test";
import { needsPlan, parseBilling, rub, tasksLeftLabel, topupShort } from "./billing.ts";

test("billing view is parsed from the server shape, rubles as numbers", () => {
  const view = parseBilling({
    plan: { id: "business", title: "Бизнес", price_rub: "1490.00", status: "active", period_ends_at: "2026-12-01T00:00:00Z", auto_renew: true },
    wallet: { balance_rub: "250.00" },
    usage: { day_percent: 34.2, month_percent: 61, blocked: false, warning: false, in_overage: false, tasks_left_month: 115, tasks_left_today: null },
    overage: { enabled: true, cap_rub: "500.00", spent_rub: "64.00", typical_task_rub: "6.18" },
    plans: [{ id: "start", title: "Старт", price_rub: "390.00", days: 30, features: ["Для кого", "1 расписание"] }],
    topup: { available: false, min_rub: "100" },
  });
  assert.equal(view.plan.status, "active");
  assert.equal(view.plan.priceRub, 1490);
  assert.equal(view.walletRub, 250);
  assert.equal(view.usage.tasksLeftMonth, 115);
  assert.equal(view.usage.tasksLeftToday, null);
  assert.equal(view.overage.typicalTaskRub, 6.18);
  assert.equal(view.plans[0].priceRub, 390);
  assert.equal(view.topup.minRub, 100);
});

test("beta accounts without a plan keep the old limits", () => {
  const view = parseBilling({ plan: { id: null, title: "Бета-тест", status: "none" }, wallet: { balance_rub: "0.00" } });
  assert.equal(view.plan.status, "none");
  assert.equal(view.plan.priceRub, null);
  assert.deepEqual(view.plans, []);
});

test("rubles and task counts read like Russian text", () => {
  assert.equal(rub(1490), "1 490 ₽");
  assert.equal(rub(64.5), "64,50 ₽");
  assert.equal(tasksLeftLabel(1, "сегодня"), "ещё ~1 задача сегодня");
  assert.equal(tasksLeftLabel(3, "сегодня"), "ещё ~3 задачи сегодня");
  assert.equal(tasksLeftLabel(1500, "в этом месяце"), "ещё ~1 500 задач в этом месяце");
  assert.equal(tasksLeftLabel(0, "сегодня"), "задачи сегодня закончились");
  assert.equal(tasksLeftLabel(null, "сегодня"), "");
});

test("a new paid-only account has no plan yet and is asked to choose one", () => {
  const view = parseBilling({
    plan: { id: "unpaid", title: "Тариф не выбран", status: "unpaid", period_ends_at: null },
    wallet: { balance_rub: "150.00" },
    usage: { blocked: true },
    topup: { available: false, min_rub: "100" },
  });
  assert.equal(view.plan.status, "unpaid");
  assert.equal(view.usage.blocked, true);
  assert.equal(needsPlan(view), true);
  assert.equal(needsPlan(parseBilling({ plan: { status: "expired" } })), true);
  assert.equal(needsPlan(parseBilling({ plan: { status: "active" } })), false);
  assert.equal(needsPlan(parseBilling({ plan: { status: "none" } })), false);
  assert.equal(needsPlan(null), false);
  // 390 ₽ plan, 150 ₽ on the wallet: put 240 ₽ more; never less than the minimum top-up.
  assert.equal(topupShort(view, 390), 240);
  assert.equal(topupShort(view, 200), 100);
  assert.equal(topupShort(view, 100), 0);
});
