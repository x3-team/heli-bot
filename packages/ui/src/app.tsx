import { useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { AppVersionCard } from "./updateNotice";
import {
  ApiError,
  type AuthResult,
  deleteCredential,
  deleteMemory,
  loadBrowser,
  loadCredentials,
  loadMe,
  openAccess,
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
  saveConsents,
  submitIncidentReport,
  submitTaskFeedback,
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
import { resolveApiBase } from "./apiBase";
import { trialLabel } from "./authSession";
import { clampComputerWidth as clampWidth, clampPercent, confirmationNeeded, limitTone } from "./behavior";
import { ComputerConfirmCard } from "./computerCard";
import { networkErrorMessage } from "./errors";
import {
  captureScreenshotBase64,
  handControlBack,
  readClientLog,
  resetAgentControl,
  tryLaunchLocalBrowser,
} from "./desktop";
import { INTEGRATION_LOGOS } from "./integrations";
import { controlRun, runAgentTask, stoppedNote, type AgentEvent } from "./agentRun";
import { StepFeed, feedItem, type FeedItem } from "./agentFeed";
import { CloudScreen, type CloudStatus } from "./cloudScreen";
import { availableClientTools, localBrowserEnabled, runClientAction } from "./clientActions";
import { Mascot, mascotLabel, type MascotState } from "./mascot";
import {
  MAX_FILE_BYTES,
  canOpenFiles,
  deliverFile,
  formatSize,
  openSavedFile,
  pendingFiles,
  revealSavedFile,
  uploadAttachment,
  type SavedFile,
} from "./files";

type View = "chat" | "limits" | "schedules" | "memory" | "settings";
type Theme = "light" | "dark";

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** User: names of the files attached to this message. */
  attachments?: string[];
  /** Assistant: a file Heli put on this computer. */
  file?: SavedFile;
}

interface Thread {
  id: string;
  title: string;
  messages: Message[];
}

export function Shell({ apiBase: apiBaseProp }: { apiBase?: string }) {
  const apiBase = resolveApiBase(apiBaseProp);
  const [theme, setTheme] = useState<Theme>(() =>
    window.localStorage.getItem("heli-theme") === "dark" ? "dark" : "light",
  );
  const [token, setToken] = useState<string | null>(() => window.sessionStorage.getItem("heli-token"));
  const [authTrialActive, setAuthTrialActive] = useState(false);
  // While the server runs without sign-up, skip the sign-in screen entirely.
  const [probingOpen, setProbingOpen] = useState(() => !window.sessionStorage.getItem("heli-token"));

  useEffect(() => {
    if (token || !probingOpen) return;
    let cancelled = false;
    void openAccess(apiBase).then((auth) => {
      if (cancelled) return;
      if (auth) completeAuth(auth);
      setProbingOpen(false);
    });
    return () => {
      cancelled = true;
    };
  }, [apiBase, token, probingOpen]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("heli-theme", theme);
  }, [theme]);

  function completeAuth(auth: AuthResult) {
    window.sessionStorage.setItem("heli-token", auth.accessToken);
    setAuthTrialActive(auth.trialActive);
    setToken(auth.accessToken);
  }

  if (!token && probingOpen) {
    return <main className="signin" />;
  }

  if (!token) {
    return (
      <SignIn
        apiBase={apiBase}
        onAuth={completeAuth}
      />
    );
  }

  return (
    <Workspace
      apiBase={apiBase}
      token={token}
      initialTrialActive={authTrialActive}
      theme={theme}
      onTheme={setTheme}
      onSignOut={() => {
        window.sessionStorage.removeItem("heli-token");
        setAuthTrialActive(false);
        setToken(null);
        setProbingOpen(true);
      }}
    />
  );
}

type AuthScreen = "register" | "signin" | "recover";

const OPERATOR_CODE_NOTICE = "Почта ещё не подключена. Введите код, который выдал оператор.";

function SignIn({ apiBase, onAuth }: { apiBase: string; onAuth: (auth: AuthResult) => void }) {
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
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  function resetFlow(next: AuthScreen) {
    setScreen(next);
    setUseCode(false);
    setSent(false);
    setCode("");
    setPassword("");
    setError("");
    setNotice("");
  }

  /** Without mail delivery the operator hands out the code, so open the code field anyway. */
  function acceptOperatorCode(err: unknown): boolean {
    if (err instanceof ApiError && err.mailNotConfigured) {
      setSent(true);
      setNotice(OPERATOR_CODE_NOTICE);
      return true;
    }
    return false;
  }

  async function askCode() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await requestCode(apiBase, email.trim(), invite || undefined);
      setSent(true);
    } catch (err) {
      if (acceptOperatorCode(err)) return;
      setError(err instanceof ApiError ? err.message : "Не получилось отправить код.");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (screen === "register" && !pdn) {
      setError("Нужно согласие на обработку данных.");
      return;
    }
    if (screen === "signin" && useCode && sent && !pdn) {
      setError("Нужно согласие на обработку данных.");
      return;
    }
    setBusy(true);
    try {
      if (screen === "register") {
        onAuth(await registerAccount(apiBase, email.trim(), password, pdn, training));
        return;
      }
      if (screen === "recover") {
        if (!sent) {
          try {
            await recoverRequest(apiBase, email.trim());
            setSent(true);
          } catch (err) {
            if (!acceptOperatorCode(err)) throw err;
          }
          return;
        }
        onAuth(await recoverReset(apiBase, email.trim(), code, password));
        return;
      }
      if (useCode) {
        if (!sent) {
          await askCode();
          return;
        }
        onAuth(await verifyAndConsent(apiBase, email.trim(), code, pdn, training));
        return;
      }
      onAuth(await loginPassword(apiBase, email.trim(), password));
    } catch (err) {
      if (err instanceof ApiError) setError(err.message);
      else if (err instanceof TypeError) setError(networkErrorMessage());
      else setError("Не получилось.");
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
        {!apiBase && !import.meta.env.DEV ? (
          <p className="error">Адрес API не задан при сборке (VITE_HELI_API_BASE).</p>
        ) : null}
        {notice && !error ? <p className="muted">{notice}</p> : null}
        {error ? <p className="error">{error}</p> : null}
        {error && apiBase && screen === "register" ? (
          <p className="muted">POST {apiBase}/v1/auth/register</p>
        ) : null}
        <button type="submit" data-emphasis="primary" disabled={busy || (!apiBase && !import.meta.env.DEV)}>
          {action}
        </button>
        {screen === "signin" ? (
          <div className="row">
            <button type="button" onClick={() => { setUseCode(!useCode); setSent(false); setError(""); setNotice(""); }}>
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
): Promise<AuthResult> {
  const access = await verifyCode(apiBase, email, code);
  // Code sign-in for an existing account must not switch off consents given earlier.
  const me = await loadMe(apiBase, access.accessToken);
  const nextTraining = me.consentTraining || training;
  if (!me.consentPdn || nextTraining !== me.consentTraining) {
    await saveConsents(apiBase, access.accessToken, me.consentPdn || pdn, nextTraining);
  }
  return access;
}

function Workspace({
  apiBase,
  token,
  initialTrialActive,
  theme,
  onTheme,
  onSignOut,
}: {
  apiBase: string;
  token: string;
  initialTrialActive?: boolean;
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
    trialActive: initialTrialActive ?? false,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [abort, setAbort] = useState<AbortController | null>(null);
  const [costHint, setCostHint] = useState<string | null>(null);
  const [feedbackTask, setFeedbackTask] = useState<{ taskId: string; reply: string } | null>(null);
  const [consents, setConsents] = useState({ pdn: true, training: false });
  const [historyOpen, setHistoryOpen] = useState(false);
  const [computerOpen, setComputerOpen] = useState(false);
  const [computerWidth, setComputerWidth] = useState<number | null>(() => readComputerWidth());
  const [, setViewport] = useState(0);
  useEffect(() => {
    // The dragged width is clamped to the window: re-render when the window changes.
    const onResize = () => setViewport(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const [runTaskId, setRunTaskId] = useState<string | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [screenTick, setScreenTick] = useState(0);
  const [paused, setPaused] = useState<{ reason: string; spent?: string; estimate?: string; text?: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [ask, setAsk] = useState<{ action: string; details: string; resolve: (ok: boolean) => void } | null>(null);
  const [autopause, setAutopause] = useState(() => readAutopause());
  const [attachments, setAttachments] = useState<File[]>([]);
  const haltedRef = useRef(false);
  const receiving = useRef(new Set<string>());
  const activeRef = useRef<string | null>(null);
  activeRef.current = activeId;

  const active = threads.find((thread) => thread.id === activeId) ?? null;
  const stopped = usage.blocked || limitTone(usage.weekPercent) === "stop" || limitTone(usage.monthPercent) === "stop";

  useEffect(() => {
    void loadUsage(apiBase, token)
      .then(setUsage)
      .catch((err) => {
        if (err instanceof ApiError) {
          setError(err.message);
          if (err.message.includes("войти снова")) {
            onSignOut();
          }
        }
      });
    void loadMe(apiBase, token)
      .then((me) => setConsents({ pdn: me.consentPdn, training: me.consentTraining }))
      .catch((err) => {
        if (err instanceof ApiError && err.message.includes("войти снова")) {
          onSignOut();
        }
      });
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

  /** Files Heli downloaded or made in the cloud: put each on this PC once and show it in the chat. */
  async function receiveFiles(threadId?: string | null) {
    const waiting = await pendingFiles(apiBase, token).catch(() => []);
    let target = threadId ?? activeRef.current;
    for (const file of waiting) {
      if (receiving.current.has(file.id)) continue;
      receiving.current.add(file.id);
      try {
        const saved = await deliverFile(apiBase, token, file);
        const message: Message = { id: crypto.randomUUID(), role: "assistant", text: "", file: saved };
        if (target) {
          setThreads((list) =>
            list.map((thread) => (thread.id === target ? { ...thread, messages: [...thread.messages, message] } : thread)),
          );
        } else {
          const created: Thread = { id: crypto.randomUUID(), title: `Файл: ${saved.name}`.slice(0, 42), messages: [message] };
          setThreads((list) => [created, ...list]);
          setActiveId(created.id);
          target = created.id;
        }
      } catch (err) {
        receiving.current.delete(file.id);
        setError(err instanceof Error ? `Не получилось сохранить «${file.name}»: ${err.message}` : "Не получилось сохранить файл.");
      }
    }
  }

  useEffect(() => {
    void receiveFiles();
  }, [apiBase, token]);

  function addAttachments(list: FileList | File[]) {
    const picked = Array.from(list);
    const tooBig = picked.find((file) => file.size > MAX_FILE_BYTES);
    if (tooBig) setError(`«${tooBig.name}» больше 25 МБ, такой файл не передать.`);
    setAttachments((current) => [...current, ...picked.filter((file) => file.size <= MAX_FILE_BYTES)].slice(0, 10));
  }

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
    setComputerOpen(true);
    const files = attachments;
    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: "user",
      text,
      attachments: files.length ? files.map((file) => file.name) : undefined,
    };
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
    setAttachments([]);
    setFeed([]);
    setPaused(null);
    setNotice("");
    haltedRef.current = false;
    try {
      const fileIds: string[] = [];
      if (files.length) {
        setNotice(files.length === 1 ? "Передаю файл на компьютер Heli…" : `Передаю файлы на компьютер Heli: ${files.length}…`);
        for (const file of files) fileIds.push((await uploadAttachment(apiBase, token, file)).id);
        setNotice("");
      }
      setMascot("working");
      const result = await runAgentTask(apiBase, token, text, {
        context: fileIds.length ? { files: fileIds } : undefined,
        clientTools: availableClientTools(),
        autopause,
        signal: controller.signal,
        onTask: setRunTaskId,
        onEvent: (event: AgentEvent) => {
          const item = feedItem(event);
          if (item) setFeed((list) => [...list, item].slice(-40));
          if (event.type === "paused") {
            const reason = String(event.reason ?? "user");
            setPaused({
              reason,
              spent: event.spent_rub ? String(event.spent_rub) : undefined,
              estimate: event.estimate_rub ? String(event.estimate_rub) : undefined,
              text: event.text ? String(event.text) : undefined,
            });
            setMascot("waiting");
            // A captcha is passed by hand on the live screen: show it.
            if (reason === "captcha") setComputerOpen(true);
          } else if (event.type === "resumed") {
            setPaused(null);
            setMascot("working");
          } else if (event.type === "screen") {
            setScreenTick((n) => n + 1);
          } else if (event.type === "file") {
            void receiveFiles(threadId);
          } else if (event.type === "notice") {
            setNotice(String(event.text ?? ""));
          } else if (event.type === "step" && event.kind === "tool_result") {
            setNotice("");
          }
        },
        onClientAction: (tool, args, callId, taskId) =>
          runClientAction(tool, args, {
            confirm: (action, details) =>
              new Promise<boolean>((resolve) => setAsk({ action, details, resolve })),
            notice: setNotice,
            halted: () => haltedRef.current,
            run: { base: apiBase, token, taskId, callId },
          }),
      });
      const note = stoppedNote(result.stopped);
      const answer: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        text: result.reply || note || "Готово.",
      };
      setThreads((list) =>
        list.map((thread) =>
          thread.id === threadId ? { ...thread, messages: [...thread.messages, answer] } : thread,
        ),
      );
      if (note && result.reply) setError(note);
      setFeedbackTask(result.reply ? { taskId: result.taskId, reply: result.reply } : null);
      setMascot(result.stopped ? "waiting" : "done");
    } catch (err) {
      if (controller.signal.aborted) {
        setMascot("waiting");
        setError("Остановлено. Управление снова у вас.");
      } else {
        setError(err instanceof ApiError ? err.message : "Не получилось выполнить задачу.");
        setMascot("error");
      }
    } finally {
      setBusy(false);
      setAbort(null);
      setRunTaskId(null);
      setPaused(null);
      setNotice("");
      setAsk((current) => {
        current?.resolve(false);
        return null;
      });
      void receiveFiles(threadId);
      void loadUsage(apiBase, token).then(setUsage).catch(() => undefined);
    }
  }

  async function togglePause() {
    if (!runTaskId) {
      void handControlBack();
      return;
    }
    if (paused) {
      void resetAgentControl();
      haltedRef.current = false;
      await controlRun(apiBase, token, runTaskId, "resume").catch(() => undefined);
      setPaused(null);
      setMascot("working");
    } else {
      void handControlBack();
      await controlRun(apiBase, token, runTaskId, "pause").catch(() => undefined);
      setPaused({ reason: "user" });
      setMascot("waiting");
    }
  }

  function stopRun() {
    haltedRef.current = true;
    void handControlBack();
    if (runTaskId) void controlRun(apiBase, token, runTaskId, "stop").catch(() => undefined);
    ask?.resolve(false);
    setAsk(null);
    abort?.abort();
  }

  function submitDraft() {
    const text = draft.trim() || (attachments.length ? "Посмотри приложенные файлы." : "");
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

  const week = clampPercent(usage.weekPercent);
  const weekTone = limitTone(week);
  const showComputer = computerOpen && view === "chat";

  return (
    <div
      className="workspace"
      data-view={view}
      data-history={historyOpen ? "open" : "closed"}
      data-computer={showComputer ? "open" : "closed"}
      style={
        computerWidth
          ? ({ "--computer-w": `${clampComputerWidth(computerWidth, historyOpen)}px` } as CSSProperties)
          : undefined
      }
    >
      <nav className="rail" aria-label="Разделы">
        <RailButton label="Новая задача" onClick={newThread} icon={ICONS.plus} />
        <RailButton
          label="Задачи"
          current={historyOpen}
          onClick={() => {
            setHistoryOpen((open) => !open);
            setView("chat");
          }}
          icon={ICONS.list}
        />
        <RailButton label="Расписания" current={view === "schedules"} onClick={() => setView("schedules")} icon={ICONS.clock} />
        <RailButton label="Память" current={view === "memory"} onClick={() => setView("memory")} icon={ICONS.memory} />
        <RailButton
          label="Компьютер агента"
          current={showComputer}
          onClick={() => {
            setComputerOpen((open) => !open);
            setView("chat");
          }}
          icon={ICONS.screen}
        />
        <span className="rail-gap" />
        <button
          type="button"
          className={`limit-ring ${weekTone}`}
          title={`Лимит недели: ${week}%${usage.trialActive ? ` · ${trialLabel(usage.trialEndsAt)}` : ""}`}
          aria-label={`Лимиты, неделя ${week}%`}
          aria-current={view === "limits" ? "page" : undefined}
          onClick={() => setView("limits")}
        >
          <svg viewBox="0 0 36 36" aria-hidden="true">
            <circle cx="18" cy="18" r="15" className="track" />
            <circle cx="18" cy="18" r="15" className="fill" strokeDasharray={`${(week / 100) * 94.2} 94.2`} />
          </svg>
          <span>{week}</span>
        </button>
        <RailButton label="Настройки" current={view === "settings"} onClick={() => setView("settings")} icon={ICONS.gear} />
      </nav>
      {historyOpen ? (
        <aside className="history" aria-label="Задачи">
          <p className="history-title">Задачи</p>
          {threads.length === 0 ? <p className="muted">Здесь появятся ваши задачи.</p> : null}
          {threads.map((thread) => (
            <button
              key={thread.id}
              type="button"
              aria-current={thread.id === activeId && view === "chat" ? "page" : undefined}
              onClick={() => {
                setActiveId(thread.id);
                setView("chat");
              }}
            >
              {thread.title}
            </button>
          ))}
        </aside>
      ) : null}
      <section className="main">
        {view === "chat" ? (
          <Chat
            title={active?.title ?? ""}
            mascot={stopped ? "waiting" : mascot}
            messages={active?.messages ?? []}
            draft={draft}
            attachments={attachments}
            onAttach={addAttachments}
            onDetach={(index) => setAttachments((list) => list.filter((_, i) => i !== index))}
            busy={busy}
            stopped={stopped}
            error={error}
            pending={pending}
            trial={usage.trialActive ? trialLabel(usage.trialEndsAt) : null}
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
                await finishTask(apiBase, token, feedbackTask.taskId, worked ? "success" : "fail", feedbackTask.reply);
                setFeedbackTask(null);
              } catch (err) {
                setError(err instanceof ApiError ? err.message : "Не получилось сохранить отзыв.");
              }
            }}
            costHint={costHint}
            paused={paused}
            notice={notice}
            ask={ask}
            onAnswer={(ok) => {
              ask?.resolve(ok);
              setAsk(null);
            }}
            onPause={() => void togglePause()}
            onStop={stopRun}
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
            autopause={autopause}
            onAutopause={(on) => {
              setAutopause(on);
              writeAutopause(on);
            }}
          />
        ) : null}
      </section>
      <Computer
        apiBase={apiBase}
        token={token}
        state={stopped ? "waiting" : mascot}
        hidden={!showComputer}
        onPause={() => void togglePause()}
        paused={paused !== null}
        captcha={paused?.reason === "captcha"}
        feed={feed}
        busy={busy}
        screenTick={screenTick}
        onInput={() => void receiveFiles()}
        onClose={() => setComputerOpen(false)}
        onResize={(width, done) => {
          const next = width === null ? null : clampComputerWidth(width, historyOpen);
          setComputerWidth(next);
          if (done) writeComputerWidth(next);
        }}
      />
    </div>
  );
}

const ICONS = {
  plus: "M12 5v14M5 12h14",
  list: "M4 6h16M4 12h16M4 18h10",
  clock: "M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16M12 8v4l3 2",
  memory: "M6 4h12v16l-6-4-6 4z",
  screen: "M4 5h16v11H4zM9 20h6M12 16v4",
  gear: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1",
  send: "M12 19V5M6 11l6-6 6 6",
  clip: "M20 11.5l-8.2 8.2a5 5 0 0 1-7.1-7.1l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9",
  close: "M6 6l12 12M18 6L6 18",
};

function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

function RailButton({ label, icon, current, onClick }: { label: string; icon: string; current?: boolean; onClick: () => void }) {
  return (
    <button type="button" className="rail-button" title={label} aria-label={label} aria-current={current ? "page" : undefined} onClick={onClick}>
      <Icon d={icon} />
    </button>
  );
}

const IDEAS = [
  "Сверь счета за октябрь в 1С с банковской выпиской",
  "Перенеси новых лидов из Битрикс24 в таблицу",
  "Разбери входящие письма в Яндекс 360 и ответь по шаблону",
];

function Chat({
  title,
  mascot,
  messages,
  draft,
  attachments,
  onAttach,
  onDetach,
  busy,
  stopped,
  error,
  pending,
  trial,
  onDraft,
  onSubmit,
  onApprove,
  onEdit,
  onReject,
  onStop,
  onPause,
  paused,
  notice,
  ask,
  onAnswer,
  costHint,
  feedback,
  onFeedback,
}: {
  title: string;
  mascot: MascotState;
  messages: Message[];
  draft: string;
  attachments: File[];
  onAttach: (files: FileList | File[]) => void;
  onDetach: (index: number) => void;
  busy: boolean;
  stopped: boolean;
  error: string;
  pending: string | null;
  trial: string | null;
  costHint: string | null;
  feedback: { taskId: string; reply: string } | null;
  onDraft: (value: string) => void;
  onSubmit: () => void;
  onApprove: () => void;
  onEdit: () => void;
  onReject: () => void;
  onStop: () => void;
  onPause: () => void;
  paused: { reason: string; spent?: string; estimate?: string; text?: string } | null;
  notice: string;
  ask: { action: string; details: string } | null;
  onAnswer: (ok: boolean) => void;
  onFeedback: (worked: boolean, comment: string) => Promise<void>;
}) {
  const [comment, setComment] = useState("");
  const threadEnd = useRef<HTMLDivElement>(null);
  const filePicker = useRef<HTMLInputElement>(null);
  const empty = messages.length === 0 && !pending && !stopped;

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: "end" });
  }, [messages.length, pending, feedback, busy, ask, paused, notice]);

  const composer = (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) event.preventDefault();
      }}
      onDrop={(event) => {
        if (!event.dataTransfer.files.length || stopped) return;
        event.preventDefault();
        onAttach(event.dataTransfer.files);
      }}
    >
      {attachments.length ? (
        <ul className="attachments" aria-label="Приложенные файлы">
          {attachments.map((file, index) => (
            <li key={`${file.name}-${index}`}>
              <span aria-hidden="true">📎</span> {file.name} <span className="muted">{formatSize(file.size)}</span>
              <button type="button" className="chip-x" aria-label={`Убрать ${file.name}`} title="Убрать" onClick={() => onDetach(index)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="composer-box">
        <input
          ref={filePicker}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            if (event.target.files?.length) onAttach(event.target.files);
            event.target.value = "";
          }}
        />
        <button
          type="button"
          className="attach-button"
          title="Приложить файл"
          aria-label="Приложить файл"
          disabled={stopped || busy}
          onClick={() => filePicker.current?.click()}
        >
          <Icon d={ICONS.clip} />
        </button>
        <textarea
          aria-label="Текст задачи"
          placeholder={stopped ? "Лимит исчерпан до сброса" : "Опишите задачу"}
          rows={1}
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
          <button type="button" className="round-button" title="Стоп" aria-label="Стоп" onClick={onStop}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" />
            </svg>
          </button>
        ) : (
          <button type="submit" className="round-button" title="Отправить" aria-label="Отправить" disabled={stopped || (!draft.trim() && attachments.length === 0)}>
            <Icon d={ICONS.send} />
          </button>
        )}
      </div>
      {costHint && !busy ? <p className="composer-hint">{costHint}</p> : null}
    </form>
  );

  if (empty) {
    return (
      <div className="chat-empty">
        <div className="hero-mascot">
          <Mascot state="waiting" size={72} />
        </div>
        <h1>Привет! Чем займёмся?</h1>
        {composer}
        <div className="ideas">
          {IDEAS.map((idea) => (
            <button key={idea} type="button" onClick={() => onDraft(idea)}>
              {idea}
            </button>
          ))}
        </div>
        <p className="composer-hint">
          Опасные действия Heli спросит заранее · Ctrl+Alt+Пробел ставит на паузу
          {trial ? ` · ${trial}` : ""}
        </p>
        {error ? <p className="error">{error}</p> : null}
      </div>
    );
  }

  return (
    <>
      <header className="chat-head">
        <span className="chat-title">{title}</span>
        <span className={`live ${mascot}`}>{mascotLabel(mascot)}</span>
      </header>
      <div className="thread">
        {messages.map((message) =>
          message.file ? (
            <FileCard key={message.id} file={message.file} />
          ) : message.role === "assistant" ? (
            <div key={message.id} className="bubble assistant">
              <span className="bubble-avatar" aria-hidden="true">
                <Mascot state="waiting" size={28} />
              </span>
              <div>{message.text}</div>
            </div>
          ) : (
            <div key={message.id} className="bubble user">
              {message.text}
              {message.attachments?.length ? (
                <span className="bubble-files">
                  {message.attachments.map((name) => (
                    <span key={name}>📎 {name}</span>
                  ))}
                </span>
              ) : null}
            </div>
          ),
        )}
        {busy ? (
          <div className="bubble assistant typing">
            <span className="bubble-avatar" aria-hidden="true">
              <Mascot state="thinking" size={28} />
            </span>
            <div className="muted">{mascot === "working" ? "Лечу по задаче на компьютере агента…" : "Прогреваю винт…"}</div>
          </div>
        ) : null}
        {pending ? (
          <article className="card danger">
            <p className="muted">Нужно ваше разрешение</p>
            <h2>Выполнить это действие?</h2>
            <p>{pending}</p>
            <div className="row">
              <button type="button" data-emphasis="primary" onClick={onApprove}>Разрешить</button>
              <button type="button" onClick={onEdit}>Изменить</button>
              <button type="button" data-emphasis="danger" onClick={onReject}>Отклонить</button>
            </div>
          </article>
        ) : null}
        {ask ? (
          <article className="card danger">
            <p className="muted">Heli спрашивает разрешение</p>
            <h2>{ask.action || "Выполнить действие?"}</h2>
            {ask.details ? <p>{ask.details}</p> : null}
            <div className="row">
              <button type="button" data-emphasis="primary" onClick={() => onAnswer(true)}>Разрешить</button>
              <button type="button" data-emphasis="danger" onClick={() => onAnswer(false)}>Отклонить</button>
            </div>
          </article>
        ) : null}
        {paused?.reason === "cost" ? (
          <article className="card">
            <p className="muted">Автопауза</p>
            <h2>Задача дороже оценки</h2>
            <p>
              Потрачено {formatRub(paused.spent)} при оценке {formatRub(paused.estimate)}. Продолжить?
            </p>
            <div className="row">
              <button type="button" data-emphasis="primary" onClick={onPause}>Продолжить</button>
              <button type="button" data-emphasis="danger" onClick={onStop}>Стоп</button>
            </div>
          </article>
        ) : null}
        {paused?.reason === "captcha" ? (
          <article className="card">
            <p className="muted">Нужна ваша помощь</p>
            <h2>Пройдите капчу</h2>
            <p>{paused.text || "Сайт просит пройти капчу. Пройдите её на экране «Компьютер агента», и я продолжу сам."}</p>
            <div className="row">
              <button type="button" data-emphasis="primary" onClick={onPause}>Готово, продолжай</button>
              <button type="button" data-emphasis="danger" onClick={onStop}>Стоп</button>
            </div>
          </article>
        ) : null}
        {notice ? <p className="muted">{notice}</p> : null}
        <ComputerConfirmCard />
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
        <div ref={threadEnd} />
      </div>
      {composer}
    </>
  );
}

function FileCard({ file }: { file: SavedFile }) {
  const [note, setNote] = useState("");
  const desktop = canOpenFiles() && Boolean(file.path);
  async function act(action: () => Promise<void>) {
    setNote("");
    try {
      await action();
    } catch (err) {
      setNote(String(err) === "risky_file" ? "Программы и скрипты Heli не открывает." : "Не получилось открыть.");
    }
  }
  return (
    <article className="card file-card">
      <p className="muted">{desktop ? "Файл на вашем компьютере" : "Файл скачан браузером"}</p>
      <h2>{file.name}</h2>
      <p className="muted">
        {desktop ? "Загрузки › Heli · " : ""}
        {formatSize(file.size)}
      </p>
      {file.risky ? (
        <p className="muted">Это программа или скрипт. Heli такие файлы не запускает: откройте сами, если доверяете источнику.</p>
      ) : null}
      {desktop ? (
        <div className="row">
          {file.risky ? null : (
            <button type="button" data-emphasis="primary" onClick={() => void act(() => openSavedFile(file.path ?? ""))}>
              Открыть
            </button>
          )}
          <button type="button" onClick={() => void act(() => revealSavedFile(file.path))}>
            Показать в папке
          </button>
        </div>
      ) : null}
      {note ? <p className="error">{note}</p> : null}
    </article>
  );
}

function Computer({
  apiBase,
  token,
  state,
  hidden,
  busy,
  paused,
  captcha,
  feed,
  screenTick,
  onPause,
  onInput,
  onClose,
  onResize,
}: {
  apiBase: string;
  token: string;
  state: MascotState;
  hidden: boolean;
  busy: boolean;
  paused: boolean;
  /** The task waits for the user to pass a captcha on the live screen. */
  captcha: boolean;
  feed: FeedItem[];
  screenTick: number;
  onPause: () => void;
  onInput: () => void;
  onClose: () => void;
  /** New panel width while dragging (done = the drag ended); null resets it. */
  onResize: (width: number | null, done: boolean) => void;
}) {
  const panel = useRef<HTMLElement>(null);
  const drag = useRef<{ x: number; width: number; last: number } | null>(null);
  const [cloud, setCloud] = useState<CloudStatus | null>(null);
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
    <aside className="computer" aria-label="Компьютер агента" hidden={hidden} ref={panel}>
      <div
        className="computer-resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="Ширина панели: тяните влево или вправо, двойной щелчок вернёт как было"
        title="Тяните, чтобы изменить ширину. Двойной щелчок вернёт как было."
        tabIndex={0}
        onPointerDown={(event) => {
          const width = panel.current?.getBoundingClientRect().width ?? 0;
          drag.current = { x: event.clientX, width, last: width };
          event.currentTarget.setPointerCapture(event.pointerId);
          event.preventDefault();
        }}
        onPointerMove={(event) => {
          if (!drag.current) return;
          drag.current.last = drag.current.width + (drag.current.x - event.clientX);
          onResize(drag.current.last, false);
        }}
        onPointerUp={(event) => {
          if (!drag.current) return;
          onResize(drag.current.last, true);
          drag.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onDoubleClick={() => onResize(null, true)}
        onKeyDown={(event) => {
          const width = panel.current?.getBoundingClientRect().width ?? 0;
          if (event.key === "ArrowLeft") onResize(width + 40, true);
          else if (event.key === "ArrowRight") onResize(width - 40, true);
          else return;
          event.preventDefault();
        }}
      />
      <div className="computer-in">
        <div className="computer-top">
          <b>Компьютер агента</b>
          <span className={`live ${state}`}>{mascotLabel(state)}</span>
          <span className="computer-actions">
            {busy ? (
              <button type="button" className="pill" onClick={onPause}>{paused ? "Продолжить" : "Пауза"}</button>
            ) : null}
            <button type="button" className="pill icon" title="Свернуть панель" aria-label="Свернуть панель" onClick={onClose}>
              <Icon d={ICONS.close} />
            </button>
          </span>
        </div>
        {captcha ? (
          <p className="computer-ask">
            Пройдите капчу здесь: нажмите на поле или картинку и печатайте с клавиатуры. Я продолжу сам, когда она пропадёт.
          </p>
        ) : null}
        <CloudScreen
          apiBase={apiBase}
          token={token}
          tick={screenTick}
          busy={busy}
          focus={captcha}
          onStatus={setCloud}
          onInput={onInput}
          placeholder={<Mascot state={state} />}
        />
        {cloud?.enabled ? null : (
          <div className="screen">
            <Mascot state={state} />
          </div>
        )}
        <StepFeed items={feed} />
        {cloud?.enabled && !localBrowserEnabled() ? null : (
        <div className="computer-foot">
          <span className="muted">
            {browser.status === "running"
              ? browser.mode === "local"
                ? `Локальный ${browser.localBrowser ?? "браузер"} · порт ${browser.localPort ?? "—"}`
                : "Облачный браузер"
              : "Браузер выключен"}
          </span>
          <span className="computer-actions">
            {browser.novncUrl ? (
              <a className="pill" href={browser.novncUrl} target="_blank" rel="noreferrer">Смотреть</a>
            ) : null}
            <button type="button" className="pill" onClick={() => void toggle()}>
              {browser.status === "running" ? "Остановить браузер" : "Запустить браузер"}
            </button>
          </span>
        </div>
        )}
        {note ? <p className="error">{note}</p> : null}
      </div>
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
        <article className="card">{trialLabel(usage.trialEndsAt)}. Лимиты показаны в процентах.</article>
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
  autopause,
  onAutopause,
}: {
  apiBase: string;
  token: string;
  theme: Theme;
  consents: { pdn: boolean; training: boolean };
  onTheme: (theme: Theme) => void;
  onConsents: (next: { pdn: boolean; training: boolean }) => Promise<void>;
  onSignOut: () => void;
  autopause: boolean;
  onAutopause: (on: boolean) => void;
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
        <h2>Задачи</h2>
        <label className="check">
          <input type="checkbox" checked={autopause} onChange={(event) => onAutopause(event.target.checked)} />
          <span>Ставить задачу на паузу, если она стоит больше чем в 1,5 раза дороже оценки</span>
        </label>
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
      <AppVersionCard />
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

const AUTOPAUSE_KEY = "heli.autopause";
const COMPUTER_WIDTH_KEY = "heli.computerWidth";

function clampComputerWidth(width: number, historyOpen: boolean): number {
  return clampWidth(width, historyOpen, window.innerWidth);
}

/** The width the user dragged the «Компьютер агента» panel to; null: the default. */
function readComputerWidth(): number | null {
  try {
    const value = Number(window.localStorage.getItem(COMPUTER_WIDTH_KEY));
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

function writeComputerWidth(width: number | null): void {
  try {
    if (width === null) window.localStorage.removeItem(COMPUTER_WIDTH_KEY);
    else window.localStorage.setItem(COMPUTER_WIDTH_KEY, String(width));
  } catch {
    // per-device convenience only
  }
}

function readAutopause(): boolean {
  try {
    return window.localStorage.getItem(AUTOPAUSE_KEY) !== "off";
  } catch {
    return true;
  }
}

function writeAutopause(on: boolean): void {
  try {
    window.localStorage.setItem(AUTOPAUSE_KEY, on ? "on" : "off");
  } catch {
    // per-device convenience only
  }
}

function formatRub(value?: string): string {
  const n = Number.parseFloat(value ?? "");
  return Number.isFinite(n) ? `${n.toFixed(2)} ₽` : "—";
}
