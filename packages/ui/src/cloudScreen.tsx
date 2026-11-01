/**
 * Live screen of Heli's own browser on the server, in the «Компьютер агента» panel.
 * The user watches the agent work and can click and type on the screen: sign in to a service once
 * (the sign-in is kept on the server for this account) or pass a captcha.
 */

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { readApiError } from "./errors";

export interface CloudStatus {
  enabled: boolean;
  running: boolean;
  version: number;
  url: string;
  title: string;
  saved_logins: boolean;
}

const OFF: CloudStatus = { enabled: false, running: false, version: 0, url: "", title: "", saved_logins: false };

type Input =
  | { kind: "click"; x: number; y: number }
  | { kind: "text"; text: string }
  | { kind: "key"; key: string }
  | { kind: "scroll"; dy: number }
  | { kind: "open"; url: string }
  | { kind: "back" }
  | { kind: "refresh" };

/** Keys the server accepts; everything printable goes as text. */
export const SPECIAL_KEYS = new Set([
  "Enter", "Escape", "Tab", "Backspace", "Delete", "PageDown", "PageUp", "ArrowDown", "ArrowUp",
  "ArrowLeft", "ArrowRight", "Home", "End",
]);

function headers(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

export async function loadCloudStatus(base: string, token: string): Promise<CloudStatus> {
  const response = await fetch(`${base}/v1/cloud-browser`, { headers: headers(token) });
  if (!response.ok) return OFF;
  return (await response.json()) as CloudStatus;
}

async function post(base: string, token: string, path: string, body?: unknown): Promise<CloudStatus> {
  const response = await fetch(`${base}/v1/cloud-browser/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers(token) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await readApiError(response));
  return { enabled: true, ...((await response.json()) as Omit<CloudStatus, "enabled">) };
}

export function sendCloudInput(base: string, token: string, input: Input): Promise<CloudStatus> {
  return post(base, token, "input", input);
}

/** Relative click position on the screen image, 0..1 on both axes. */
export function relativePoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
): { x: number; y: number } {
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  return { x: clamp((clientX - rect.left) / rect.width), y: clamp((clientY - rect.top) / rect.height) };
}

export function CloudScreen({
  apiBase,
  token,
  tick,
  busy,
  onStatus,
  onInput,
  placeholder,
}: {
  apiBase: string;
  token: string;
  /** Bumped by the run's `screen` events. */
  tick: number;
  /** A task is running: refresh more often. */
  busy: boolean;
  onStatus?: (status: CloudStatus) => void;
  /** After each click or key of the user: a click may have downloaded a file. */
  onInput?: () => void;
  /** Shown until the first frame: the mascot. */
  placeholder?: ReactNode;
}) {
  const [status, setStatus] = useState<CloudStatus>(OFF);
  const [image, setImage] = useState<string | null>(null);
  const [address, setAddress] = useState("");
  const [note, setNote] = useState("");
  const shownVersion = useRef(0);
  const typed = useRef("");
  const flushTimer = useRef<number | null>(null);
  const wheel = useRef({ dy: 0, timer: null as number | null });

  const apply = useCallback(
    (next: CloudStatus) => {
      setStatus(next);
      onStatus?.(next);
    },
    [onStatus],
  );

  const refreshImage = useCallback(async () => {
    const response = await fetch(`${apiBase}/v1/cloud-browser/screen`, { headers: headers(token) });
    if (!response.ok) return;
    const version = Number(response.headers.get("x-screen-version") ?? 0);
    const blob = await response.blob();
    shownVersion.current = version;
    setImage(URL.createObjectURL(blob));
  }, [apiBase, token]);

  const poll = useCallback(async () => {
    const next = await loadCloudStatus(apiBase, token).catch(() => OFF);
    apply(next);
    if (next.enabled && next.version > 0 && next.version !== shownVersion.current) await refreshImage();
  }, [apiBase, token, apply, refreshImage]);

  useEffect(() => {
    void poll();
  }, [poll, tick]);

  useEffect(() => {
    const id = window.setInterval(() => void poll(), busy ? 1500 : 5000);
    return () => window.clearInterval(id);
  }, [poll, busy]);

  useEffect(() => () => {
    if (image) URL.revokeObjectURL(image);
  }, [image]);

  async function send(input: Input) {
    setNote("");
    try {
      apply(await sendCloudInput(apiBase, token, input));
      await refreshImage();
      onInput?.();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Браузер на сервере сейчас недоступен.");
    }
  }

  function flushTyped() {
    if (flushTimer.current !== null) window.clearTimeout(flushTimer.current);
    flushTimer.current = null;
    const text = typed.current;
    typed.current = "";
    if (text) void send({ kind: "text", text });
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key.length === 1) {
      event.preventDefault();
      typed.current += event.key;
      if (flushTimer.current !== null) window.clearTimeout(flushTimer.current);
      flushTimer.current = window.setTimeout(flushTyped, 300);
      return;
    }
    if (SPECIAL_KEYS.has(event.key)) {
      event.preventDefault();
      flushTyped();
      void send({ kind: "key", key: event.key });
    }
  }

  function onClick(event: MouseEvent<HTMLImageElement>) {
    flushTyped();
    const point = relativePoint(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect());
    void send({ kind: "click", ...point });
  }

  if (!status.enabled) return null;

  return (
    <div className="cloud">
      <form
        className="cloud-bar"
        onSubmit={(event) => {
          event.preventDefault();
          if (address.trim()) void send({ kind: "open", url: address.trim() });
        }}
      >
        <button type="button" title="Назад" aria-label="Назад" onClick={() => void send({ kind: "back" })}>
          ←
        </button>
        <input
          value={address}
          onChange={(event) => setAddress(event.target.value)}
          placeholder={status.url || "Адрес сайта, чтобы войти"}
          aria-label="Адрес"
        />
        <button type="submit">Открыть</button>
      </form>
      <div
        className="cloud-screen"
        tabIndex={0}
        onKeyDown={onKeyDown}
        onPaste={(event) => {
          const text = event.clipboardData.getData("text");
          if (text) {
            event.preventDefault();
            flushTyped();
            void send({ kind: "text", text });
          }
        }}
        onWheel={(event) => {
          wheel.current.dy += event.deltaY;
          if (wheel.current.timer !== null) return;
          wheel.current.timer = window.setTimeout(() => {
            const dy = Math.round(Math.max(-3000, Math.min(3000, wheel.current.dy)));
            wheel.current = { dy: 0, timer: null };
            if (dy) void send({ kind: "scroll", dy });
          }, 250);
        }}
        aria-label="Экран облачного браузера. Нажмите, чтобы кликнуть, и печатайте с клавиатуры."
      >
        {image ? (
          <img src={image} alt={status.title || "Экран облачного браузера"} onClick={onClick} draggable={false} />
        ) : (
          <div className="cloud-empty">
            {placeholder}
            <button type="button" onClick={() => void send({ kind: "refresh" })}>
              Включить экран
            </button>
          </div>
        )}
      </div>
      <p className="muted cloud-hint">
        {status.running ? "Облачный браузер Heli. " : "Браузер спит. "}
        Входы в сервисы, сделанные здесь, сохраняются.
        {status.saved_logins ? (
          <>
            {" "}
            <button
              type="button"
              className="link"
              onClick={() => {
                if (window.confirm("Выйти из всех сайтов в облачном браузере?")) void post(apiBase, token, "forget").then(apply);
              }}
            >
              Выйти из всех сайтов
            </button>
          </>
        ) : null}
      </p>
      {note ? <p className="error">{note}</p> : null}
    </div>
  );
}
