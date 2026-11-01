import { useEffect, useState, useSyncExternalStore } from "react";
import { appVersion, prepareAppUpdate, type ReadyUpdate } from "./desktop";

const RECHECK_MS = 4 * 60 * 60 * 1000;

type Status = "idle" | "checking" | "latest" | "ready" | "error" | "web";
type State = { status: Status; ready: ReadyUpdate | null; error: string; checkedAt: number | null };

// One shared state, so the corner card and the settings card show the same thing.
let state: State = { status: "idle", ready: null, error: "", checkedAt: null };
const listeners = new Set<() => void>();
let inFlight: Promise<void> | null = null;

function set(next: Partial<State>) {
  state = { ...state, ...next };
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function checkForUpdate(): Promise<void> {
  if (state.ready) return Promise.resolve();
  if (inFlight) return inFlight;
  set({ status: "checking", error: "" });
  inFlight = prepareAppUpdate().then((result) => {
    inFlight = null;
    const checkedAt = Date.now();
    if (result.kind === "ready") set({ status: "ready", ready: result.update, checkedAt });
    else if (result.kind === "error") set({ status: "error", error: result.message, checkedAt });
    else set({ status: result.kind, checkedAt });
  });
  return inFlight;
}

function useUpdateState(): State {
  return useSyncExternalStore(subscribe, () => state);
}

function useInstall(ready: ReadyUpdate | null) {
  const [installing, setInstalling] = useState(false);
  const [failed, setFailed] = useState(false);
  async function install() {
    if (!ready) return;
    setInstalling(true);
    setFailed(false);
    try {
      await ready.install();
    } catch {
      setInstalling(false);
      setFailed(true);
    }
  }
  return { installing, failed, install };
}

/** Small card in the corner: a newer Heli is downloaded, one click restarts into it. */
export function UpdateNotice() {
  const { ready } = useUpdateState();
  const [hidden, setHidden] = useState(false);
  const { installing, failed, install } = useInstall(ready);

  useEffect(() => {
    void checkForUpdate();
    const timer = window.setInterval(() => void checkForUpdate(), RECHECK_MS);
    return () => window.clearInterval(timer);
  }, []);

  if (!ready || hidden) return null;

  return (
    <div className="update-notice" role="status">
      <span className="dot" aria-hidden="true" />
      <div className="update-text">
        <b>Вышла новая версия Heli</b>
        <span>{failed ? "Не получилось обновиться. Попробуйте ещё раз." : installing ? "Обновляю, Heli откроется сам…" : `Версия ${ready.version} уже скачана`}</span>
      </div>
      <button type="button" className="update-go" disabled={installing} onClick={() => void install()}>
        {installing ? "Обновляю…" : "Обновить"}
      </button>
      {installing ? null : (
        <button type="button" className="update-later" aria-label="Позже" title="Позже" onClick={() => setHidden(true)}>
          ×
        </button>
      )}
    </div>
  );
}

/** Settings card: which version is installed and a manual «Проверить обновления». */
export function AppVersionCard() {
  const { status, ready, error } = useUpdateState();
  const [version, setVersion] = useState<string | null>(null);
  const { installing, failed, install } = useInstall(ready);

  useEffect(() => {
    void appVersion().then(setVersion);
  }, []);

  if (!version) return null;

  let line = "";
  if (status === "checking") line = "Проверяю обновления…";
  else if (status === "latest") line = "У вас последняя версия.";
  else if (status === "ready" && ready) line = failed ? "Не получилось обновиться. Попробуйте ещё раз." : `Скачана версия ${ready.version}.`;
  else if (status === "error") line = `Не удалось проверить обновления: ${error}`;

  return (
    <section className="card">
      <h2>О приложении</h2>
      <p>Heli bot, версия {version}</p>
      {line ? <p className="muted">{line}</p> : null}
      <div className="row">
        {ready ? (
          <button type="button" data-emphasis="primary" disabled={installing} onClick={() => void install()}>
            {installing ? "Обновляю…" : `Обновить до ${ready.version}`}
          </button>
        ) : (
          <button type="button" disabled={status === "checking"} onClick={() => void checkForUpdate()}>
            Проверить обновления
          </button>
        )}
      </div>
    </section>
  );
}
