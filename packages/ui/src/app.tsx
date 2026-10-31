import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  deleteCredential,
  loadBrowser,
  loadCredentials,
  loadMe,
  loadUsage,
  requestCode,
  runTask,
  saveConsents,
  saveCredential,
  startBrowser,
  stopBrowser,
  verifyCode,
  type BrowserView,
  type CredentialItem,
  type Usage,
} from "./api";
import { clampPercent, confirmationNeeded, limitTone } from "./behavior";
import { Mascot, avatarFor, mascotLabel, type MascotState } from "./mascot";

type View = "chat" | "limits" | "settings";
type Theme = "light" | "dark";

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
}

interface Thread {
  id: string;
  title: string;
  messages: Message[];
}

export function Shell({ apiBase = "" }: { apiBase?: string }) {
  const [theme, setTheme] = useState<Theme>(() =>
    window.localStorage.getItem("heli-theme") === "dark" ? "dark" : "light",
  );
  const [token, setToken] = useState<string | null>(() => window.sessionStorage.getItem("heli-token"));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("heli-theme", theme);
  }, [theme]);

  if (!token) {
    return (
      <SignIn
        apiBase={apiBase}
        onToken={(value) => {
          window.sessionStorage.setItem("heli-token", value);
          setToken(value);
        }}
      />
    );
  }

  return (
    <Workspace
      apiBase={apiBase}
      token={token}
      theme={theme}
      onTheme={setTheme}
      onSignOut={() => {
        window.sessionStorage.removeItem("heli-token");
        setToken(null);
      }}
    />
  );
}

function SignIn({ apiBase, onToken }: { apiBase: string; onToken: (token: string) => void }) {
  const [email, setEmail] = useState("");
  const [invite, setInvite] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [pdn, setPdn] = useState(false);
  const [training, setTraining] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function askCode() {
    setBusy(true);
    setError("");
    try {
      await requestCode(apiBase, email, invite);
      setSent(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не получилось отправить код.");
    } finally {
      setBusy(false);
    }
  }

  async function enter() {
    if (!pdn) {
      setError("Нужно согласие на обработку данных.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const access = await verifyAndConsent(apiBase, email, code, pdn, training);
      onToken(access);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не получилось войти.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="signin">
      <form
        className="signin-card"
        onSubmit={(event) => {
          event.preventDefault();
          if (sent) void enter();
          else void askCode();
        }}
      >
        <Mascot state="waiting" size={120} />
        <div>
          <h1>Heli bot</h1>
          <p className="muted">Вход по приглашению</p>
        </div>
        <label className="stack">
          Почта
          <input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" required />
        </label>
        <label className="stack">
          Код приглашения
          <input value={invite} onChange={(event) => setInvite(event.target.value)} required />
        </label>
        {sent ? (
          <label className="stack">
            Код из письма
            <input value={code} onChange={(event) => setCode(event.target.value)} inputMode="numeric" required />
          </label>
        ) : null}
        <label className="check">
          <input type="checkbox" checked={pdn} onChange={(event) => setPdn(event.target.checked)} />
          <span>Согласие на обработку персональных данных</span>
        </label>
        <label className="check">
          <input type="checkbox" checked={training} onChange={(event) => setTraining(event.target.checked)} />
          <span>Использовать мои задачи для улучшения модели</span>
        </label>
        {error ? <p className="error">{error}</p> : null}
        <button type="submit" data-emphasis="primary" disabled={busy}>
          {sent ? "Войти" : "Получить код"}
        </button>
      </form>
    </main>
  );
}

async function verifyAndConsent(
  apiBase: string,
  email: string,
  code: string,
  pdn: boolean,
  training: boolean,
): Promise<string> {
  const access = await verifyCode(apiBase, email, code);
  await saveConsents(apiBase, access, pdn, training);
  return access;
}

function Workspace({
  apiBase,
  token,
  theme,
  onTheme,
  onSignOut,
}: {
  apiBase: string;
  token: string;
  theme: Theme;
  onTheme: (theme: Theme) => void;
  onSignOut: () => void;
}) {
  const [view, setView] = useState<View>("chat");
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [mascot, setMascot] = useState<MascotState>("waiting");
  const [usage, setUsage] = useState<Usage>({ weekPercent: 0, monthPercent: 0, blocked: false, warning: false });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [abort, setAbort] = useState<AbortController | null>(null);
  const [consents, setConsents] = useState({ pdn: true, training: false });

  const active = threads.find((thread) => thread.id === activeId) ?? null;
  const stopped = usage.blocked || limitTone(usage.weekPercent) === "stop" || limitTone(usage.monthPercent) === "stop";

  useEffect(() => {
    void loadUsage(apiBase, token).then(setUsage).catch(() => undefined);
    void loadMe(apiBase, token)
      .then((me) => setConsents({ pdn: me.consentPdn, training: me.consentTraining }))
      .catch(() => undefined);
  }, [apiBase, token]);

  function newThread() {
    const thread: Thread = { id: crypto.randomUUID(), title: "Новая задача", messages: [] };
    setThreads((list) => [thread, ...list]);
    setActiveId(thread.id);
    setView("chat");
    setMascot("waiting");
    setPending(null);
  }

  async function send(text: string, threadId: string) {
    if (stopped) return;
    const controller = new AbortController();
    setAbort(controller);
    setBusy(true);
    setError("");
    setMascot("thinking");
    const userMessage: Message = { id: crypto.randomUUID(), role: "user", text };
    setThreads((list) =>
      list.map((thread) =>
        thread.id === threadId
          ? {
              ...thread,
              title: thread.messages.length === 0 ? text.slice(0, 42) : thread.title,
              messages: [...thread.messages, userMessage],
            }
          : thread,
      ),
    );
    setDraft("");
    try {
      setMascot("working");
      const reply = await runTask(apiBase, token, text, controller.signal);
      const answer: Message = { id: crypto.randomUUID(), role: "assistant", text: reply };
      setThreads((list) =>
        list.map((thread) =>
          thread.id === threadId ? { ...thread, messages: [...thread.messages, answer] } : thread,
        ),
      );
      setMascot("done");
    } catch (err) {
      if (controller.signal.aborted) {
        setMascot("waiting");
      } else {
        setError(err instanceof ApiError ? err.message : "Не получилось выполнить задачу.");
        setMascot("waiting");
      }
    } finally {
      setBusy(false);
      setAbort(null);
      void loadUsage(apiBase, token).then(setUsage).catch(() => undefined);
    }
  }

  function submitDraft() {
    const text = draft.trim();
    if (!text || busy || stopped) return;
    let threadId = active?.id;
    if (!threadId) {
      threadId = crypto.randomUUID();
      const created: Thread = { id: threadId, title: text.slice(0, 42), messages: [] };
      setThreads((list) => [created, ...list]);
      setActiveId(threadId);
    }
    if (confirmationNeeded(text)) {
      setPending(text);
      return;
    }
    void send(text, threadId);
  }

  const avatar = useMemo(() => avatarFor(theme), [theme]);

  return (
    <div className="workspace" data-view={view}>
      <aside className="sidebar">
        <div className="brand">
          <img src={avatar} alt="" />
          Heli bot
        </div>
        <button type="button" data-emphasis="primary" onClick={newThread}>
          Новая задача
        </button>
        <div className="chat-list">
          {threads.map((thread) => (
            <button
              key={thread.id}
              type="button"
              aria-current={thread.id === activeId ? "page" : undefined}
              onClick={() => {
                setActiveId(thread.id);
                setView("chat");
              }}
            >
              {thread.title}
            </button>
          ))}
        </div>
        <nav className="nav grow">
          <button type="button" aria-current={view === "chat" ? "page" : undefined} onClick={() => setView("chat")}>
            Чат
          </button>
          <button type="button" aria-current={view === "limits" ? "page" : undefined} onClick={() => setView("limits")}>
            Лимиты
          </button>
          <button type="button" aria-current={view === "settings" ? "page" : undefined} onClick={() => setView("settings")}>
            Настройки
          </button>
        </nav>
      </aside>
      <section className="main">
        {view === "chat" ? (
          <Chat
            messages={active?.messages ?? []}
            draft={draft}
            busy={busy}
            stopped={stopped}
            error={error}
            pending={pending}
            onDraft={setDraft}
            onSubmit={submitDraft}
            onApprove={() => {
              const text = pending;
              const threadId = active?.id;
              setPending(null);
              if (text && threadId) void send(text, threadId);
            }}
            onEdit={() => {
              if (pending) setDraft(pending);
              setPending(null);
            }}
            onReject={() => setPending(null)}
            onStop={() => abort?.abort()}
          />
        ) : null}
        {view === "limits" ? <Limits usage={usage} /> : null}
        {view === "settings" ? (
          <Settings
            apiBase={apiBase}
            token={token}
            theme={theme}
            consents={consents}
            onTheme={onTheme}
            onConsents={async (next) => {
              await saveConsents(apiBase, token, next.pdn, next.training);
              setConsents(next);
            }}
            onSignOut={onSignOut}
          />
        ) : null}
      </section>
      <Computer apiBase={apiBase} token={token} state={stopped ? "waiting" : mascot} />
    </div>
  );
}

function Chat({
  messages,
  draft,
  busy,
  stopped,
  error,
  pending,
  onDraft,
  onSubmit,
  onApprove,
  onEdit,
  onReject,
  onStop,
}: {
  messages: Message[];
  draft: string;
  busy: boolean;
  stopped: boolean;
  error: string;
  pending: string | null;
  onDraft: (value: string) => void;
  onSubmit: () => void;
  onApprove: () => void;
  onEdit: () => void;
  onReject: () => void;
  onStop: () => void;
}) {
  return (
    <>
      <div className="thread">
        {messages.length === 0 ? <p className="muted">Опишите задачу. Опасные действия спросят подтверждение.</p> : null}
        {messages.map((message) => (
          <div key={message.id} className={`bubble ${message.role}`}>
            {message.text}
          </div>
        ))}
        {pending ? (
          <article className="card danger">
            <p className="muted">Подтверждение</p>
            <h2>Выполнить это действие?</h2>
            <p>{pending}</p>
            <div className="row">
              <button type="button" data-emphasis="primary" onClick={onApprove}>Разрешить</button>
              <button type="button" onClick={onEdit}>Изменить</button>
              <button type="button" data-emphasis="danger" onClick={onReject}>Отклонить</button>
            </div>
          </article>
        ) : null}
        {stopped ? (
          <article className="card danger">
            <h2>Лимит исчерпан</h2>
            <p>Неделя или месяц на 100%. Новые задачи остановлены до сброса.</p>
          </article>
        ) : null}
        {error ? <p className="error">{error}</p> : null}
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <textarea
          aria-label="Текст задачи"
          value={draft}
          disabled={stopped}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
        />
        {busy ? (
          <button type="button" onClick={onStop}>Стоп</button>
        ) : (
          <button type="submit" data-emphasis="primary" disabled={stopped || !draft.trim()}>
            Отправить
          </button>
        )}
      </form>
    </>
  );
}

function Computer({ apiBase, token, state }: { apiBase: string; token: string; state: MascotState }) {
  const [browser, setBrowser] = useState<BrowserView>({ status: "stopped", novncUrl: null });
  const [note, setNote] = useState("");

  useEffect(() => {
    void loadBrowser(apiBase, token).then(setBrowser).catch(() => undefined);
  }, [apiBase, token]);

  async function toggle() {
    setNote("");
    try {
      const next = browser.status === "running"
        ? await stopBrowser(apiBase, token)
        : await startBrowser(apiBase, token);
      setBrowser(next);
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Браузер на сервере сейчас недоступен.");
    }
  }

  return (
    <aside className="computer">
      <div className="status">
        <h2>Компьютер агента</h2>
        <span>{mascotLabel(state)}</span>
      </div>
      <div className="screen">
        <Mascot state={state} />
      </div>
      <div className="row">
        <button type="button" onClick={() => void toggle()}>
          {browser.status === "running" ? "Остановить браузер" : "Запустить браузер"}
        </button>
        {browser.novncUrl ? (
          <a href={browser.novncUrl} target="_blank" rel="noreferrer">Смотреть</a>
        ) : null}
      </div>
      {note ? <p className="error">{note}</p> : null}
    </aside>
  );
}

function Limits({ usage }: { usage: Usage }) {
  const week = clampPercent(usage.weekPercent);
  const month = clampPercent(usage.monthPercent);
  const weekTone = limitTone(week);
  const monthTone = limitTone(month);
  const warned = usage.warning || weekTone === "warn" || monthTone === "warn";
  return (
    <div className="page">
      <div>
        <h1>Лимиты</h1>
        <p className="muted">Использовано {week}% недели · {month}% месяца, сброс пн 00:00 МСК</p>
      </div>
      {warned ? <article className="card warn">Осталось меньше 20% лимита. Сложные шаги будут проще.</article> : null}
      {(weekTone === "stop" || monthTone === "stop") ? (
        <article className="card danger">
          <h2>Лимит исчерпан</h2>
          <p>Жёсткая остановка на 100%.</p>
        </article>
      ) : null}
      <Meter label="Неделя" percent={week} tone={weekTone} />
      <Meter label="Месяц" percent={month} tone={monthTone} />
    </div>
  );
}

function Meter({ label, percent, tone }: { label: string; percent: number; tone: string }) {
  return (
    <div className="meter">
      <div className="status">
        <strong>{label}</strong>
        <span>{percent}%</span>
      </div>
      <div className={`bar ${tone}`} aria-hidden="true">
        <span style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function Settings({
  apiBase,
  token,
  theme,
  consents,
  onTheme,
  onConsents,
  onSignOut,
}: {
  apiBase: string;
  token: string;
  theme: Theme;
  consents: { pdn: boolean; training: boolean };
  onTheme: (theme: Theme) => void;
  onConsents: (next: { pdn: boolean; training: boolean }) => Promise<void>;
  onSignOut: () => void;
}) {
  const [site, setSite] = useState("");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [links, setLinks] = useState<CredentialItem[]>([]);
  const [note, setNote] = useState("");

  useEffect(() => {
    void loadCredentials(apiBase, token).then(setLinks).catch(() => undefined);
  }, [apiBase, token]);

  return (
    <div className="page">
      <h1>Настройки</h1>
      <section className="card">
        <h2>Тема</h2>
        <div className="row">
          <button type="button" aria-pressed={theme === "light"} onClick={() => onTheme("light")}>Светлая</button>
          <button type="button" aria-pressed={theme === "dark"} onClick={() => onTheme("dark")}>Тёмная</button>
        </div>
      </section>
      <section className="card">
        <h2>Согласия</h2>
        <label className="check">
          <input
            type="checkbox"
            checked={consents.pdn}
            onChange={(event) => {
              void onConsents({ ...consents, pdn: event.target.checked }).catch(() => undefined);
            }}
          />
          <span>Согласие на обработку персональных данных</span>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={consents.training}
            onChange={(event) => {
              void onConsents({ ...consents, training: event.target.checked }).catch(() => undefined);
            }}
          />
          <span>Использовать мои задачи для улучшения модели</span>
        </label>
      </section>
      <section className="card">
        <h2>Подключения</h2>
        <p className="muted">Сайт и имя видны вам. Секрет шифруется и в ответы не попадает.</p>
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            const nextSite = site.trim();
            const nextUsername = username.trim();
            const nextSecret = secret;
            if (!nextSite || !nextUsername || !nextSecret) return;
            setSite("");
            setUsername("");
            setSecret("");
            void saveCredential(apiBase, token, nextSite, nextUsername, nextSecret)
              .then((saved) => {
                setLinks((list) => [...list, saved]);
                setNote("Секрет зашифрован и в ответы не попадает.");
              })
              .catch((err: unknown) => {
                setNote(err instanceof ApiError ? err.message : "Не получилось сохранить.");
              });
          }}
        >
          <input aria-label="Сайт" placeholder="Сайт" value={site} onChange={(event) => setSite(event.target.value)} />
          <input aria-label="Имя" placeholder="Имя" value={username} onChange={(event) => setUsername(event.target.value)} />
          <input
            aria-label="Секрет"
            placeholder="Секрет"
            type="password"
            value={secret}
            autoComplete="new-password"
            onChange={(event) => setSecret(event.target.value)}
          />
          <button type="submit">Добавить</button>
        </form>
        {note ? <p className="muted">{note}</p> : null}
        <ul>
          {links.map((link) => (
            <li key={link.id}>
              {link.site} · {link.username}
              <button
                type="button"
                onClick={() => {
                  void deleteCredential(apiBase, token, link.id).then(() => {
                    setLinks((list) => list.filter((item) => item.id !== link.id));
                  }).catch(() => undefined);
                }}
              >
                Удалить
              </button>
            </li>
          ))}
        </ul>
      </section>
      <button type="button" onClick={onSignOut}>Выйти</button>
    </div>
  );
}
