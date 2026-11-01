import { useEffect, useRef, useState } from "react";
import { prepareAppUpdate, type ReadyUpdate } from "./desktop";

const RECHECK_MS = 4 * 60 * 60 * 1000;

/** Small card in the corner: a newer Heli is downloaded, one click restarts into it. */
export function UpdateNotice() {
  const [ready, setReady] = useState<ReadyUpdate | null>(null);
  const [installing, setInstalling] = useState(false);
  const [failed, setFailed] = useState(false);
  const [hidden, setHidden] = useState(false);
  const checking = useRef(false);

  useEffect(() => {
    let alive = true;
    async function look() {
      if (checking.current) return;
      checking.current = true;
      const found = await prepareAppUpdate();
      checking.current = false;
      if (alive && found) {
        setReady(found);
        setHidden(false);
      }
    }
    void look();
    const timer = window.setInterval(() => void look(), RECHECK_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  if (!ready || hidden) return null;

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
