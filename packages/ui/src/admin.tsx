import { useEffect, useState } from "react";
import {
  ApiError,
  adminCreateInvite,
  adminIssueCode,
  adminListUsers,
  adminSetLimits,
  loadMe,
  type AdminUser,
} from "./api";

/** Settings block for the admin: invites for new testers and codes while mail is not wired. */
export function AdminTesters({ apiBase, token }: { apiBase: string; token: string }) {
  const [isAdmin, setIsAdmin] = useState(false);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [invite, setInvite] = useState("");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [limitUser, setLimitUser] = useState("");
  const [weekly, setWeekly] = useState("");
  const [monthly, setMonthly] = useState("");

  useEffect(() => {
    let cancelled = false;
    void loadMe(apiBase, token)
      .then((me) => {
        if (cancelled || !me.isAdmin) return;
        setIsAdmin(true);
        return adminListUsers(apiBase, token).then((list) => {
          if (!cancelled) setUsers(list);
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [apiBase, token]);

  if (!isAdmin) return null;

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не получилось.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" data-testid="admin-testers">
      <h2>Тестеры</h2>
      <p className="muted">
        Новый тестер регистрируется по коду приглашения. Код одноразовый, отправьте его тестеру сами.
      </p>
      <div className="row">
        <button
          type="button"
          data-emphasis="primary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              setInvite(await adminCreateInvite(apiBase, token));
            })
          }
        >
          Создать приглашение
        </button>
        {invite ? <code data-testid="invite-code">{invite}</code> : null}
      </div>
      <label className="stack">
        Почта тестера
        <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" />
      </label>
      <p className="muted">Почта ещё не подключена, поэтому код входа и код сброса пароля выдаёте вы. Код живёт 10 минут.</p>
      <div className="row">
        <button
          type="button"
          disabled={busy || !email.trim()}
          onClick={() =>
            void run(async () => {
              setCode(await adminIssueCode(apiBase, token, email, "login"));
            })
          }
        >
          Код для входа
        </button>
        <button
          type="button"
          disabled={busy || !email.trim()}
          onClick={() =>
            void run(async () => {
              setCode(await adminIssueCode(apiBase, token, email, "reset"));
            })
          }
        >
          Код сброса пароля
        </button>
        {code ? <code data-testid="issued-code">{code}</code> : null}
      </div>
      {error ? <p className="error">{error}</p> : null}
      {users.length ? (
        <ul className="muted" data-testid="admin-users">
          {users.map((user) => (
            <li key={user.id}>
              {user.email}
              {user.isAdmin ? " · админ" : ""}
              {user.isTraining ? " · учебный" : ""}
              {user.trialActive ? " · пробная неделя" : ""}
              {user.monthlyLimitRub
                ? ` · неделя ${Math.round(Number(user.weeklyLimitRub))} ₽ (${user.weekPercent}%), месяц ${Math.round(
                    Number(user.monthlyLimitRub),
                  )} ₽ (${user.monthPercent}%)`
                : ""}
            </li>
          ))}
        </ul>
      ) : null}
      {users.length ? (
        <div className="stack" data-testid="admin-limits">
          <label className="stack">
            Лимиты пользователя
            <select value={limitUser} onChange={(event) => setLimitUser(event.target.value)}>
              <option value="">Выберите аккаунт</option>
              {users.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.email}
                </option>
              ))}
            </select>
          </label>
          <div className="row">
            <input
              value={weekly}
              onChange={(event) => setWeekly(event.target.value)}
              inputMode="decimal"
              placeholder="Неделя, ₽ (0 — авто)"
            />
            <input
              value={monthly}
              onChange={(event) => setMonthly(event.target.value)}
              inputMode="decimal"
              placeholder="Месяц, ₽"
            />
            <button
              type="button"
              disabled={busy || !limitUser || (!weekly.trim() && !monthly.trim())}
              onClick={() =>
                void run(async () => {
                  await adminSetLimits(apiBase, token, limitUser, {
                    weeklyRub: weekly.trim() ? Number(weekly.replace(",", ".")) : undefined,
                    monthlyRub: monthly.trim() ? Number(monthly.replace(",", ".")) : undefined,
                  });
                  setUsers(await adminListUsers(apiBase, token));
                  setWeekly("");
                  setMonthly("");
                })
              }
            >
              Сохранить лимиты
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
