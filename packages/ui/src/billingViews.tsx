import { useEffect, useState, type CSSProperties } from "react";
import {
  BillingError,
  choosePlan,
  dayLabel,
  loadWalletHistory,
  requestTopup,
  rub,
  saveBillingSettings,
  tasksLeftLabel,
  type Billing,
  type PlanOffer,
  type WalletOperation,
} from "./billing";
import { clampPercent, limitTone } from "./behavior";
import { openLink } from "./desktop";
import { PageHead } from "./pageHead";

/** Manager who issues invoices until card payments are connected (same contact as on getheli.ru). */
export const MANAGER_TELEGRAM = "https://t.me/Bo_wot";
const OVERAGE_CAP_MAX = 5000;
const OVERAGE_CAP_STEP = 100;

function errorText(err: unknown, fallback: string): string {
  return err instanceof BillingError ? err.message : fallback;
}

function Bar({ percent, dark }: { percent: number; dark?: boolean }) {
  const value = clampPercent(percent);
  return (
    <div className={`bar ${limitTone(value)}${dark ? " on-dark" : ""}`} aria-hidden="true">
      <span style={{ width: `${value}%` }} />
    </div>
  );
}

function currentLine(billing: Billing): string {
  const { plan } = billing;
  const until = dayLabel(plan.periodEndsAt);
  if (plan.status === "none") return "Тестовый доступ: лимиты как раньше, оплата не нужна.";
  if (plan.status === "trial") return `Бесплатно до ${until}. Потом можно выбрать тариф.`;
  if (plan.status === "expired") return `Тариф закончился ${until}. Новые задачи ждут продления.`;
  const price = plan.priceRub !== null ? `${rub(plan.priceRub)} в месяц · ` : "";
  return plan.autoRenew ? `${price}продлится ${until} из кошелька` : `${price}действует до ${until}`;
}

/** «Тариф»: current plan, today's and this month's limit in percent, the three plans. */
export function PlanPage({
  apiBase,
  token,
  billing,
  onBilling,
  onWallet,
}: {
  apiBase: string;
  token: string;
  billing: Billing;
  onBilling: (next: Billing) => void;
  onWallet: (topup?: number) => void;
}) {
  const [confirm, setConfirm] = useState<PlanOffer | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; short?: number } | null>(null);
  const { plan, usage } = billing;
  const hasPlan = plan.status !== "none";

  async function buy(offer: PlanOffer) {
    setBusy(true);
    setNote(null);
    try {
      onBilling(await choosePlan(apiBase, token, offer.id));
      setConfirm(null);
      setNote({ text: `Готово: тариф «${offer.title}» включён.` });
    } catch (err) {
      const short = err instanceof BillingError && err.code === "insufficient_funds" ? Math.max(0, offer.priceRub - billing.walletRub) : undefined;
      setNote({ text: errorText(err, "Не получилось сменить тариф. Попробуйте позже."), short });
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page billing">
      <PageHead
        title="Тариф"
        lede="Лимит считается в процентах: видно, сколько осталось на сегодня и на месяц. На 100% новые задачи ждут нового дня или идут за счёт кошелька, если вы это разрешили."
      />
      <div className="billing-top">
        <section className="bill-card dark">
          <span className="bill-label">Ваш тариф</span>
          <strong className="bill-big">{plan.title}</strong>
          <span className="bill-sub">{currentLine(billing)}</span>
        </section>
        <section className="bill-card">
          <span className="bill-label">Сегодня</span>
          <span className="bill-value">
            <strong className="bill-big">{clampPercent(usage.dayPercent)}%</strong>
            <span>обновится в 00:00</span>
          </span>
          <Bar percent={usage.dayPercent} />
          <span className="bill-sub">{tasksLeftLabel(usage.tasksLeftToday, "сегодня")}</span>
        </section>
        <section className="bill-card">
          <span className="bill-label">Месяц</span>
          <span className="bill-value">
            <strong className="bill-big">{clampPercent(usage.monthPercent)}%</strong>
            {usage.periodEndsAt ? <span>до {dayLabel(usage.periodEndsAt)}</span> : null}
          </span>
          <Bar percent={usage.monthPercent} />
          <span className="bill-sub">{usage.inOverage ? "Лимит месяца закончился, задачи идут за счёт кошелька" : tasksLeftLabel(usage.tasksLeftMonth, "в этом месяце")}</span>
        </section>
      </div>
      {note ? (
        <div className={`bill-note${note.short !== undefined ? " warn" : ""}`} role="status">
          <span>{note.text}</span>
          {note.short !== undefined ? (
            <button type="button" data-emphasis="go" onClick={() => onWallet(Math.max(billing.topup.minRub, Math.ceil(note.short ?? 0)))}>
              Пополнить кошелёк
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="plans">
        {billing.plans.map((offer) => {
          const current = hasPlan && plan.id === offer.id && plan.status !== "expired";
          const [who, ...features] = offer.features;
          return (
            <article key={offer.id} className={`plan${current ? " current" : ""}`}>
              <header>
                <h2>{offer.title}</h2>
                {current ? <span className="chip go">ваш тариф</span> : null}
              </header>
              {who ? <p className="plan-who">{who}</p> : null}
              <p className="plan-price">
                <strong>{rub(offer.priceRub)}</strong> в месяц
              </p>
              <ul>
                {features.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
              {current ? (
                <button type="button" className="plan-cta" disabled>
                  {plan.autoRenew ? `Продлится ${dayLabel(plan.periodEndsAt)}` : `До ${dayLabel(plan.periodEndsAt)}`}
                </button>
              ) : (
                <button
                  type="button"
                  className="plan-cta"
                  data-emphasis={offer.id === "pro" ? "go" : undefined}
                  disabled={busy}
                  onClick={() => setConfirm(offer)}
                >
                  {plan.id === offer.id ? `Продлить «${offer.title}»` : `Перейти на «${offer.title}»`}
                </button>
              )}
            </article>
          );
        })}
      </div>
      <p className="muted small">
        Тариф оплачивается из кошелька Heli bot. При смене тарифа неиспользованные дни старого возвращаются в кошелёк. Пробная неделя бесплатна.
      </p>
      {confirm ? (
        <Dialog title={`Тариф «${confirm.title}»`} onClose={() => setConfirm(null)}>
          <p className="muted">
            Спишем {rub(confirm.priceRub)} с кошелька за {confirm.days} дней.
            {hasPlan && plan.status === "active" && plan.id !== confirm.id ? " Неиспользованные дни текущего тарифа вернём в кошелёк." : ""}
          </p>
          <div className="bill-from">
            <span>На кошельке</span>
            <strong>{rub(billing.walletRub)}</strong>
          </div>
          <div className="dialog-actions">
            <button type="button" onClick={() => setConfirm(null)}>
              Отмена
            </button>
            <button type="button" data-emphasis="primary" disabled={busy} onClick={() => void buy(confirm)}>
              {busy ? "Подключаю…" : `Оплатить ${rub(confirm.priceRub)}`}
            </button>
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

/** «Кошелёк»: balance, spending over the limit, auto-renewal, operations, top-up. */
export function WalletPage({
  apiBase,
  token,
  billing,
  email,
  onBilling,
  topupAmount,
  onTopupClose,
  onTopup,
}: {
  apiBase: string;
  token: string;
  billing: Billing;
  email: string;
  onBilling: (next: Billing) => void;
  /** Open the top-up dialog with this amount (null: closed). */
  topupAmount: number | null;
  onTopupClose: () => void;
  onTopup: () => void;
}) {
  const [ops, setOps] = useState<WalletOperation[] | null>(null);
  const [cap, setCap] = useState(billing.overage.capRub);
  const [note, setNote] = useState("");

  useEffect(() => {
    void loadWalletHistory(apiBase, token)
      .then(setOps)
      .catch(() => setOps([]));
  }, [apiBase, token, billing.walletRub]);

  useEffect(() => setCap(billing.overage.capRub), [billing.overage.capRub]);

  async function save(settings: Parameters<typeof saveBillingSettings>[2]) {
    setNote("");
    try {
      onBilling(await saveBillingSettings(apiBase, token, settings));
    } catch (err) {
      setNote(errorText(err, "Не получилось сохранить. Попробуйте позже."));
    }
  }

  const { overage } = billing;
  const spentShare = overage.capRub > 0 ? (overage.spentRub / overage.capRub) * 100 : 0;
  const hasPlan = billing.plan.status !== "none";

  return (
    <div className="page billing">
      <PageHead title="Кошелёк" lede="Отдельный кошелёк Heli bot. Из него оплачивается тариф и, если вы разрешили, задачи сверх лимита." />
      <div className="wallet-top">
        <section className="bill-card dark glow">
          <span className="bill-label">На кошельке</span>
          <strong className="bill-big huge">{rub(billing.walletRub)}</strong>
          {overage.enabled ? (
            <>
              <span className="bill-sub">
                Сверх лимита в этом месяце: {rub(overage.spentRub)} из разрешённых {rub(overage.capRub)}
              </span>
              <Bar percent={spentShare} dark />
            </>
          ) : (
            <span className="bill-sub">Сверх лимита ничего не тратится, пока вы это не включите.</span>
          )}
          <div className="bill-actions">
            <button type="button" data-emphasis="go" onClick={onTopup}>
              Пополнить
            </button>
            {hasPlan ? (
              <button type="button" className="on-dark" aria-pressed={billing.plan.autoRenew} onClick={() => void save({ autoRenew: !billing.plan.autoRenew })}>
                Продлевать тариф: {billing.plan.autoRenew ? "вкл" : "выкл"}
              </button>
            ) : null}
          </div>
        </section>
        <section className="bill-card">
          <div className="switch-row">
            <div>
              <strong>Тратить сверх лимита</strong>
              <p className="muted">Когда лимит дня или месяца закончится, задачи продолжатся за счёт кошелька. Выключено, пока вы не включите.</p>
            </div>
            <Switch label="Тратить сверх лимита" on={overage.enabled} onChange={(on) => void save({ overageEnabled: on })} />
          </div>
          <label className="cap" style={{ "--cap": `${(cap / OVERAGE_CAP_MAX) * 100}%` } as CSSProperties}>
            <strong>Готов потратить сверх лимита, не больше</strong>
            <span className="cap-row">
              <input
                type="range"
                min={0}
                max={OVERAGE_CAP_MAX}
                step={OVERAGE_CAP_STEP}
                value={cap}
                disabled={!overage.enabled}
                onChange={(event) => setCap(Number(event.target.value))}
                onPointerUp={() => void save({ overageCapRub: cap })}
                onKeyUp={() => void save({ overageCapRub: cap })}
              />
              <b>{rub(cap)}</b>
            </span>
          </label>
          {overage.typicalTaskRub !== null ? (
            <p className="muted small">Обычная задача сверх лимита стоит около {rub(Math.round(overage.typicalTaskRub))}.</p>
          ) : null}
          <p className="bill-info">Кошелёк не уходит в минус. Если деньги кончатся во время задачи, Хели её доделает, пришлёт отчёт и остановится.</p>
          {note ? <p className="error">{note}</p> : null}
        </section>
      </div>
      <section className="bill-card ops">
        <h2>Операции</h2>
        {ops === null ? <p className="muted">Загружаю…</p> : null}
        {ops?.length === 0 ? <p className="muted">Пока пусто. Здесь будут пополнения, оплата тарифа и траты сверх лимита.</p> : null}
        {ops?.map((op) => (
          <div key={op.id} className="op">
            <span className="op-date">{dayLabel(op.createdAt, true)}</span>
            <span className="op-title">{op.title}</span>
            <b className={op.amountRub > 0 ? "plus" : undefined}>
              {op.amountRub > 0 ? "+" : "−"}
              {rub(Math.abs(op.amountRub))}
            </b>
          </div>
        ))}
      </section>
      {topupAmount !== null ? (
        <TopupDialog apiBase={apiBase} token={token} billing={billing} email={email} initial={topupAmount} onClose={onTopupClose} />
      ) : null}
    </div>
  );
}

function TopupDialog({
  apiBase,
  token,
  billing,
  email,
  initial,
  onClose,
}: {
  apiBase: string;
  token: string;
  billing: Billing;
  email: string;
  initial: number;
  onClose: () => void;
}) {
  const [amount, setAmount] = useState(String(initial));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const value = Number.parseInt(amount.replace(/\D/g, ""), 10) || 0;
  const tooSmall = value < billing.topup.minRub;
  const card = billing.topup.available;

  async function go() {
    if (tooSmall) return;
    if (!card) {
      void openLink(MANAGER_TELEGRAM);
      setNote(`Напишите менеджеру: «Пополнить Heli bot на ${rub(value)}, ${email || "почта аккаунта"}». Счёт придёт в ответ, деньги появятся здесь после оплаты.`);
      return;
    }
    setBusy(true);
    try {
      const url = await requestTopup(apiBase, token, value);
      if (url) void openLink(url);
    } catch (err) {
      setNote(errorText(err, "Не получилось начать оплату. Попробуйте позже."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title="Пополнить кошелёк" onClose={onClose}>
      <p className="muted">
        {card
          ? "Картой или по СБП. После оплаты деньги сразу появятся на кошельке."
          : "Оплату картой и по СБП подключаем. Пока пополняем по счёту через менеджера: напишите сумму, пришлём счёт, деньги придут на кошелёк после оплаты."}
      </p>
      <div className="bill-from">
        <span>Кошелёк Heli bot</span>
        <strong>{rub(billing.walletRub)}</strong>
      </div>
      <label className="amount">
        <input inputMode="numeric" aria-label="Сумма пополнения" value={amount} onChange={(event) => setAmount(event.target.value)} />
        <span>₽</span>
      </label>
      <div className="quick">
        {[500, 1000, 2000, 5000].map((sum) => (
          <button key={sum} type="button" aria-pressed={value === sum} onClick={() => setAmount(String(sum))}>
            {rub(sum)}
          </button>
        ))}
      </div>
      <p className="muted small">Минимум {rub(billing.topup.minRub)}. Счёт для юрлиц и ИП — с закрывающими документами.</p>
      {note ? <p className="bill-info">{note}</p> : null}
      <div className="dialog-actions">
        <button type="button" onClick={onClose}>
          {note && !card ? "Готово" : "Отмена"}
        </button>
        <button type="button" data-emphasis="primary" disabled={tooSmall || busy} onClick={() => void go()}>
          {card ? `Оплатить ${rub(value)}` : "Написать менеджеру"}
        </button>
      </div>
    </Dialog>
  );
}

function Switch({ label, on, onChange }: { label: string; on: boolean; onChange: (on: boolean) => void }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className="switch" onClick={() => onChange(!on)}>
      <span />
    </button>
  );
}

function Dialog({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="veil"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}
