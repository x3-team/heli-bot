import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  ApiError,
  deleteCredential,
  deleteMemory,
  loadBrowser,
  loadCredentials,
  loadMe,
  loadMemory,
  loadSchedules,
  loadUsage,
  loginPassword,
  recoverRequest,
  recoverReset,
  registerAccount,
  requestCode,
  estimateTask,
  finishTask,
  runTask,
  saveConsents,
  submitIncidentReport,
  submitTaskFeedback,
  TaskAbortedError,
  attachLocalBrowser,
  saveMemory,
  saveCredential,
  saveSchedule,
  startBrowser,
  stopBrowser,
  verifyCode,
  type BrowserView,
  type CredentialItem,
  type MemoryItem,
  type ScheduleItem,
  type Usage,
} from "./api";
import { clampPercent, confirmationNeeded, limitTone } from "./behavior";
import {
  captureScreenshotBase64,
  handControlBack,
  readClientLog,
  resetAgentControl,
  tryLaunchLocalBrowser,
} from "./desktop";
import { INTEGRATION_LOGOS } from "./integrations";
import { Mascot, avatarFor, mascotLabel, type MascotState } from "./mascot";

type View = "chat" | "limits" | "schedules" | "memory" | "settings";
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

type AuthScreen = "register" | "signin" | "recover";

function SignIn({ apiBase, onToken }: { apiBase: string; onToken: (token: string) => void }) {
  const [screen, setScreen] = useState<AuthScreen>("signin");
  const [useCode, setUseCode] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [pdn, setPdn] = useState(false);
  const [training, setTraining] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function resetFlow(next: AuthScreen) {
    setScreen(next);
    setUseCode(false);
    setSent(false);
    setCode("");
    setPassword("");
    setError("");
  }

  async function askCode() {
    setBusy(true);
    setError("");
    try {
      await requestCode(apiBase, email, invite || undefined);
      setSent(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не получилось отправить код.");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (screen === "register") {
        if (!pdn) {
          setError("Нужно согласие на обработку данных.");
          return;
        }
        onToken(await registerAccount(apiBase, email, password, pdn, training));
        return;
      }
      if (screen === "recover") {
        if (!sent) {
          await recoverRequest(apiBase, email);
          setSent(true);
          return;
        }
        onToken(await recoverReset(apiBase, email, code, password));
        return;
      }
      if (useCode) {
        if (!sent) {
          await askCode();
          return;
        }
        if (!pdn) {
          setError("Нужно согласие на обработку данных.");
          return;
        }
        onToken(await verifyAndConsent(apiBase, email, code, pdn, training));
        return;
      }
      onToken(await loginPassword(apiBase, email, password));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не получилось.");
    } finally {
      setBusy(false);
    }
  }

  const title =
    screen === "register" ? "Регистрация" : screen === "recover" ? "Восстановление пароля" : "Вход";
  const subtitle =
    screen === "register"
      ? "7 дней пробного периода с лимитами по процентам"
      : screen === "recover"
        ? "Код придёт на почту (в тесте — оператор)"
        : useCode
          ? "Код на почту или по приглашению"
          : "Почта и пароль";

  let action = "Войти";
  if (screen === "register") action = "Создать аккаунт";
  else if (screen === "recover") action = sent ? "Сохранить пароль" : "Отправить код";
  else if (useCode) action = sent ? "Войти" : "Получить код";

  return (
    <main className="signin">
      <form className="signin-card" onSubmit={(event) => void submit(event)}>
        <Mascot state="waiting" size={120} />
        <div>
          <h1>Heli bot</h1>
          <p className="muted">{title} · {subtitle}</p>
        </div>
        <div className="row">
          <button type="button" aria-pressed={screen === "signin"} onClick={() => resetFlow("signin")}>
            Вход
          </button>
          <button type="button" aria-pressed={screen === "register"} onClick={() => resetFlow("register")}>
            Регистрация
          </button>
        </div>
        <label className="stack">
          Почта
          <input
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="username"
            type="email"
            required
          />
        </label>
        {screen === "signin" && !useCode ? (
          <label className="stack">
            Пароль
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              autoComplete="current-password"
              required
            />
          </label>
        ) : null}
        {screen === "register" || screen === "recover" ? (
          <label className="stack">
            {screen === "recover" && sent ? "Новый пароль" : "Пароль"}
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              autoComplete={screen === "recover" ? "new-password" : "new-password"}
              minLength={8}
              required={screen === "register" || (screen === "recover" && sent)}
            />
          </label>
        ) : null}
        {screen === "signin" && useCode ? (
          <label className="stack">
            Код приглашения (необязательно)
            <input value={invite} onChange={(event) => setInvite(event.target.value)} />
          </label>
        ) : null}
        {(screen === "signin" && useCode && sent) || (screen === "recover" && sent) ? (
          <label className="stack">
            Код из письма
            <input value={code} onChange={(event) => setCode(event.target.value)} inputMode="numeric" required />
          </label>
        ) : null}
        {screen === "register" || (screen === "signin" && useCode) ? (
          <>
            <label className="check">
              <input type="checkbox" checked={pdn} onChange={(event) => setPdn(event.target.checked)} />
              <span>Согласие на обработку персональных данных</span>
            </label>
            <label className="check">
              <input type="checkbox" checked={training} onChange={(event) => setTraining(event.target.checked)} />
              <span>Использовать мои задачи для улучшения модели</span>
            </label>
          </>
        ) : null}
        {error ? <p className="error">{error}</p> : null}
        <button type="submit" data-emphasis="primary" disabled={busy}>
          {action}
        </button>
        {screen === "signin" ? (
          <div className="row">
            <button type="button" onClick={() => { setUseCode(!useCode); setSent(false); setError(""); }}>
              {useCode ? "Войти с паролем" : "Войти по коду"}
            </button>
            <button type="button" onClick={() => resetFlow("recover")}>
              Забыли пароль?
            </button>
          </div>
        ) : null}
        {screen === "recover" ? (
          <button type="button" onClick={() => resetFlow("signin")}>
            Назад ко входу
          </button>
        ) : null}
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
  const [usage, setUsage] = useState<Usage>({
    weekPercent: 0,
    monthPercent: 0,
    blocked: false,
    warning: false,
    trialActive: false,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [abort, setAbort] = useState<AbortController | null>(null);
  const [costHint, setCostHint] = useState<string | null>(null);
  const [feedbackTask, setFeedbackTask] = useState<{ taskId: string; reply: string } | null>(null);
  const [consents, setConsents] = useState({ pdn: true, training: false });

  const active = threads.find((thread) => thread.id === activeId) ?? null;
  const stopped = usage.blocked || limitTone(usage.weekPercent) === "stop" || limitTone(usage.monthPercent) === "stop";

  useEffect(() => {
    void loadUsage(apiBase, token).then(setUsage).catch(() => undefined);
    void loadMe(apiBase, token)
      .then((me) => setConsents({ pdn: me.consentPdn, training: me.consentTraining }))
      .catch(() => undefined);
  }, [apiBase, token]);

  useEffect(() => {
    const text = draft.trim();
    if (!text || busy || stopped) {
      setCostHint(null);
      return;
    }
    const timer = window.setTimeout(() => {
      void estimateTask(apiBase, token, text)
        .then((estimate) => {
          const rub = Number.parseFloat(estimate.reserveRub);
          const label = Number.isFinite(rub) ? rub.toFixed(2) : estimate.reserveRub;
          setCostHint(`До ~${label} ₽ за шаг · ${estimate.note}`);
        })
        .catch(() => setCostHint(null));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [apiBase, token, draft, busy, stopped]);

  function newThread() {
    const thread: Thread = { id: crypto.randomUUID(), title: "Новая задача", messages: [] };
    setThreads((list) => [thread, ...list]);
    setActiveId(thread.id);
    setView("chat");
    setMascot("waiting");
    setPending(null);
    setFeedbackTask(null);
  }

  async function send(text: string, threadId: string) {
    if (stopped) return;
    setFeedbackTask(null);
    void resetAgentControl();
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
      const result = await runTask(apiBase, token, text, controller.signal);
      const answer: Message = { id: crypto.randomUUID(), role: "assistant", text: result.reply };
      setThreads((list) =>
        list.map((thread) =>
          thread.id === threadId ? { ...thread, messages: [...thread.messages, answer] } : thread,
        ),
      );
      setFeedbackTask(result);
      setMascot("done");
    } catch (err) {
      if (err instanceof TaskAbortedError || controller.signal.aborted) {
        setMascot("waiting");
        setError("Остановлено. Управление снова у вас.");
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
          <button type="button" aria-current={view === "schedules" ? "page" : undefined} onClick={() => setView("schedules")}>
            Расписания
          </button>
          <button type="button" aria-current={view === "memory" ? "page" : undefined} onClick={() => setView("memory")}>
            Память
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
            feedback={feedbackTask}
            onFeedback={async (worked, comment) => {
              if (!feedbackTask) return;
              try {
                await submitTaskFeedback(apiBase, token, feedbackTask.taskId, worked, comment);
                await finishTask(apiBase, token, feedbackTask.taskId, "success", feedbackTask.reply);
                setFeedbackTask(null);
              } catch (err) {
                setError(err instanceof ApiError ? err.message : "Не получилось сохранить отзыв.");
              }
            }}
            costHint={costHint}
            onPause={() => {
              void handControlBack();
            }}
            onStop={() => {
              void handControlBack();
              abort?.abort();
            }}
          />
        ) : null}
        {view === "limits" ? <Limits usage={usage} /> : null}
        {view === "schedules" ? <Schedules apiBase={apiBase} token={token} /> : null}
        {view === "memory" ? <Memory apiBase={apiBase} token={token} /> : null}
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
  onPause,
  onStop,
  costHint,
  feedback,
  onFeedback,
}: {
  messages: Message[];
  draft: string;
  busy: boolean;
  stopped: boolean;
  error: string;
  pending: string | null;
  costHint: string | null;
  feedback: { taskId: string; reply: string } | null;
  onDraft: (value: string) => void;
  onSubmit: () => void;
  onApprove: () => void;
  onEdit: () => void;
  onReject: () => void;
  onPause: () => void;
  onStop: () => void;
  onFeedback: (worked: boolean, comment: string) => Promise<void>;
}) {
  const [comment, setComment] = useState("");
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
        {feedback ? (
          <article className="card">
            <p className="muted">Как прошла задача?</p>
            <textarea
              aria-label="Комментарий"
              placeholder="Комментарий (необязательно)"
              value={comment}
              onChange={(event) => setComment(event.target.value)}
            />
            <div className="row">
              <button type="button" data-emphasis="primary" onClick={() => void onFeedback(true, comment)}>
                Сработало
              </button>
              <button type="button" onClick={() => void onFeedback(false, comment)}>
                Не сработало
              </button>
            </div>
          </article>
        ) : null}
        {error ? <p className="error">{error}</p> : null}
        {costHint && !busy ? <p className="muted">{costHint}</p> : null}
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
          <div className="row">
            <button type="button" onClick={onPause}>Пауза</button>
            <button type="button" data-emphasis="danger" onClick={onStop}>Стоп</button>
          </div>
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
  const [browser, setBrowser] = useState<BrowserView>({
    status: "stopped",
    mode: "cloud",
    novncUrl: null,
    localBrowser: null,
    localPort: null,
  });
  const [note, setNote] = useState("");

  useEffect(() => {
    void loadBrowser(apiBase, token).then(setBrowser).catch(() => undefined);
  }, [apiBase, token]);

  async function toggle() {
    setNote("");
    try {
      if (browser.status === "running") {
        setBrowser(await stopBrowser(apiBase, token));
        return;
      }
      const launch = await tryLaunchLocalBrowser();
      if (launch) {
        setBrowser(await attachLocalBrowser(apiBase, token, launch.browser, launch.port));
        return;
      }
      try {
        setBrowser(await startBrowser(apiBase, token, true));
      } catch (err) {
        if (err instanceof ApiError && err.message === "Нужен локальный Chrome или Edge на этом компьютере.") {
          setBrowser(await startBrowser(apiBase, token, false));
          setNote("Запущен облачный браузер. В приложении предпочтителен ваш Chrome или Edge.");
          return;
        }
        throw err;
      }
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
      {browser.status === "running" ? (
        <p className="muted">
          {browser.mode === "local"
            ? `Локальный ${browser.localBrowser ?? "браузер"} · порт ${browser.localPort ?? "—"}`
            : "Облачный браузер"}
        </p>
      ) : null}
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
      {usage.trialActive ? (
        <article className="card">Пробная неделя активна — лимиты показаны в процентах.</article>
      ) : null}
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

function Memory({ apiBase, token }: { apiBase: string; token: string }) {
  const [facts, setFacts] = useState<MemoryItem[]>([]);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    void loadMemory(apiBase, token).then(setFacts).catch(() => undefined);
  }, [apiBase, token]);

  return (
    <div className="page">
      <h1>Память</h1>
      <p className="muted">Факты подставляются в задачу. Их можно удалить.</p>
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault();
          const next = text.trim();
          if (!next) return;
          setText("");
          void saveMemory(apiBase, token, next)
            .then((fact) => setFacts((list) => [...list, fact]))
            .catch((err: unknown) => {
              setNote(err instanceof ApiError ? err.message : "Не получилось сохранить.");
            });
        }}
      >
        <textarea aria-label="Факт" placeholder="Факт о компании" value={text} onChange={(event) => setText(event.target.value)} />
        <button type="submit" data-emphasis="primary">Запомнить</button>
      </form>
      {note ? <p className="error">{note}</p> : null}
      <ul>
        {facts.map((fact) => (
          <li key={fact.id}>
            {fact.text}
            <button
              type="button"
              onClick={() => {
                void deleteMemory(apiBase, token, fact.id).then(() => {
                  setFacts((list) => list.filter((item) => item.id !== fact.id));
                }).catch(() => undefined);
              }}
            >
              Удалить
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Schedules({ apiBase, token }: { apiBase: string; token: string }) {
  const [rows, setRows] = useState<ScheduleItem[]>([]);
  const [label, setLabel] = useState("");
  const [when, setWhen] = useState("каждый понедельник в 9:00");
  const [request, setRequest] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    void loadSchedules(apiBase, token).then(setRows).catch(() => undefined);
  }, [apiBase, token]);

  return (
    <div className="page">
      <h1>Расписания</h1>
      <p className="muted">Запуск идёт в облаке и списывает лимит. Результат остаётся в задаче.</p>
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault();
          if (!label.trim() || !when.trim() || !request.trim()) return;
          void saveSchedule(apiBase, token, label.trim(), when.trim(), request.trim())
            .then((row) => {
              setRows((list) => [...list, row]);
              setLabel("");
              setRequest("");
              setNote("");
            })
            .catch((err: unknown) => {
              setNote(err instanceof ApiError ? err.message : "Не получилось сохранить расписание.");
            });
        }}
      >
        <input aria-label="Название" placeholder="Название" value={label} onChange={(event) => setLabel(event.target.value)} />
        <input aria-label="Когда" value={when} onChange={(event) => setWhen(event.target.value)} />
        <textarea aria-label="Задача" placeholder="Что сделать" value={request} onChange={(event) => setRequest(event.target.value)} />
        <button type="submit" data-emphasis="primary">Добавить</button>
      </form>
      {note ? <p className="error">{note}</p> : null}
      <ul>
        {rows.map((row) => (
          <li key={row.id}>
            <strong>{row.label}</strong> · {row.expression}
            {row.lastResult ? <span> · {row.lastResult}</span> : null}
            {row.lastError === "limit_reached" ? <span> · Лимит исчерпан</span> : null}
          </li>
        ))}
      </ul>
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
  const [reportBusy, setReportBusy] = useState(false);

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
        <ul className="integration-logos" aria-label="Поддерживаемые сервисы">
          {INTEGRATION_LOGOS.map((logo) => (
            <li key={logo.id}>
              <span className="integration-chip" title={logo.name}>
                <img src={logo.src} alt="" />
              </span>
            </li>
          ))}
        </ul>
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
      <section className="card">
        <h2>Сообщить об ошибке</h2>
        <p className="muted">Отправим снимок экрана и журнал клиента. Пароли и ключи не включаются.</p>
        <button
          type="button"
          data-emphasis="primary"
          disabled={reportBusy}
          onClick={() => {
            setReportBusy(true);
            setNote("");
            void (async () => {
              try {
                const shot = await captureScreenshotBase64();
                await submitIncidentReport(
                  apiBase,
                  token,
                  "Сообщение из приложения Heli bot",
                  readClientLog(),
                  shot,
                );
                setNote("Отчёт отправлен. Спасибо!");
              } catch (err) {
                setNote(err instanceof ApiError ? err.message : "Не получилось отправить отчёт.");
              } finally {
                setReportBusy(false);
              }
            })();
          }}
        >
          Отправить отчёт
        </button>
      </section>
      <button type="button" onClick={onSignOut}>Выйти</button>
    </div>
  );
}
