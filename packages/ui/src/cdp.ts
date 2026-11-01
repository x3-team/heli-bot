/**
 * Minimal Chrome DevTools Protocol client for the agent's browser tools.
 * Runs in the desktop webview (and in Node for tests): needs only a global WebSocket.
 * Chrome is launched by the desktop shell with --remote-allow-origins for the app origin.
 */

type Pending = { resolve: (value: any) => void; reject: (err: Error) => void };

export class CdpSession {
  private ws: WebSocket;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private listeners = new Map<string, Array<(params: any) => void>>();
  private closed = false;

  private constructor(ws: WebSocket) {
    this.ws = ws;
    ws.onmessage = (event) => this.onMessage(String(event.data));
    ws.onclose = () => {
      this.closed = true;
      for (const p of this.pending.values()) p.reject(new Error("browser_disconnected"));
      this.pending.clear();
    };
  }

  static connect(wsUrl: string, timeoutMs = 10_000): Promise<CdpSession> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(wsUrl);
      const timer = setTimeout(() => {
        ws.close();
        reject(new Error("cdp_timeout"));
      }, timeoutMs);
      ws.onopen = () => {
        clearTimeout(timer);
        resolve(new CdpSession(ws));
      };
      ws.onerror = () => {
        clearTimeout(timer);
        reject(new Error("cdp_connect_failed"));
      };
    });
  }

  get isOpen(): boolean {
    return !this.closed;
  }

  send<T = any>(method: string, params: Record<string, unknown> = {}, timeoutMs = 30_000): Promise<T> {
    if (this.closed) return Promise.reject(new Error("browser_disconnected"));
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`cdp_timeout:${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  once(event: string, timeoutMs: number): Promise<any | null> {
    return new Promise((resolve) => {
      const list = this.listeners.get(event) ?? [];
      const timer = setTimeout(() => {
        remove();
        resolve(null);
      }, timeoutMs);
      const handler = (params: any) => {
        clearTimeout(timer);
        remove();
        resolve(params);
      };
      const remove = () => {
        const current = this.listeners.get(event) ?? [];
        this.listeners.set(
          event,
          current.filter((h) => h !== handler),
        );
      };
      list.push(handler);
      this.listeners.set(event, list);
    });
  }

  close(): void {
    try {
      this.ws.close();
    } catch {
      // ignore
    }
  }

  private onMessage(raw: string): void {
    let message: any;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (typeof message.id === "number") {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(String(message.error.message ?? "cdp_error")));
      else pending.resolve(message.result);
      return;
    }
    if (typeof message.method === "string") {
      for (const handler of this.listeners.get(message.method) ?? []) handler(message.params);
    }
  }
}

/** Page snapshot script: numbers visible interactive elements with data-heli-ref. */
const SNAPSHOT = `(() => {
  const MAX_TEXT = 6000, MAX_ELEMENTS = 150;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const label = (el) => {
    const secret = (el.getAttribute("type") || "").toLowerCase() === "password";
    const t = (el.getAttribute("aria-label") || el.innerText || (secret ? "" : el.value) || el.getAttribute("placeholder")
      || el.getAttribute("title") || el.getAttribute("name") || el.getAttribute("alt") || "").trim();
    return t.replace(/\\s+/g, " ").slice(0, 80);
  };
  const kind = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "ссылка";
    if (tag === "button" || el.getAttribute("role") === "button") return "кнопка";
    if (tag === "select") return "список";
    if (tag === "textarea" || el.isContentEditable) return "поле";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset"].includes(type)) return "кнопка";
      if (type === "checkbox" || type === "radio") return "флажок";
      if (type === "password") return "поле пароля";
      return "поле";
    }
    return el.getAttribute("role") || tag;
  };
  document.querySelectorAll("[data-heli-ref]").forEach((el) => el.removeAttribute("data-heli-ref"));
  const nodes = document.querySelectorAll(
    "a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=tab], [role=menuitem], [contenteditable=true]"
  );
  const elements = [];
  for (const el of nodes) {
    if (elements.length >= MAX_ELEMENTS) break;
    if (!visible(el)) continue;
    const ref = elements.length + 1;
    el.setAttribute("data-heli-ref", String(ref));
    elements.push({ ref, kind: kind(el), label: label(el) });
  }
  const text = (document.body ? document.body.innerText : "").replace(/\\n{3,}/g, "\\n\\n").slice(0, MAX_TEXT);
  return JSON.stringify({ url: location.href, title: document.title, text, elements });
})()`;

const CAPTCHA = `(() => /showcaptcha|checkcaptcha|captcha/i.test(location.pathname)
  || /Подтвердите, что запросы отправляли вы|Я не робот|SmartCaptcha/i.test(document.body ? document.body.innerText.slice(0, 3000) : ""))()`;

/** Organic results from a Yandex results page; skips ads and Yandex's own services. */
const SERP = `(() => {
  const out = [], seen = new Set();
  const items = document.querySelectorAll("li.serp-item, .serp-item, [data-cid]");
  const pick = (root) => {
    if (root.querySelector(".label_theme_direct, [data-fast-name='direct'], .AdvLabel")) return;
    const a = root.querySelector("a.OrganicTitle-Link, a.organic__url, h2 a, a[href^='http']");
    if (!a) return;
    let href = a.href;
    try { const u = new URL(href); if (/(^|\\.)yandex\\.|(^|\\.)ya\\.ru$/.test(u.hostname)) return; } catch { return; }
    if (seen.has(href)) return;
    seen.add(href);
    const title = (root.querySelector("h2, .OrganicTitle-LinkText") || a).innerText.trim().replace(/\\s+/g, " ");
    const snip = root.querySelector(".OrganicTextContentSpan, .organic__content-wrapper, .text-container, .Organic-ContentWrapper");
    out.push({ title: title.slice(0, 200), url: href, snippet: (snip ? snip.innerText : "").trim().replace(/\\s+/g, " ").slice(0, 300) });
  };
  items.forEach((el) => out.length < 8 && pick(el));
  return JSON.stringify(out);
})()`;

export interface PageSnapshot {
  url: string;
  title: string;
  text: string;
  elements: Array<{ ref: number; kind: string; label: string }>;
}

export function formatSnapshot(snap: PageSnapshot): string {
  const lines = [`Адрес: ${snap.url}`, `Заголовок: ${snap.title}`, "", snap.text.trim() || "(текста нет)"];
  if (snap.elements.length) {
    lines.push("", "Элементы:");
    for (const el of snap.elements) lines.push(`[${el.ref}] ${el.kind}${el.label ? ` «${el.label}»` : ""}`);
  }
  return lines.join("\n");
}

const KEYS: Record<string, { code: string; vk: number; text?: string }> = {
  Enter: { code: "Enter", vk: 13, text: "\r" },
  Escape: { code: "Escape", vk: 27 },
  Tab: { code: "Tab", vk: 9 },
  Backspace: { code: "Backspace", vk: 8 },
  PageDown: { code: "PageDown", vk: 34 },
  PageUp: { code: "PageUp", vk: 33 },
  ArrowDown: { code: "ArrowDown", vk: 40 },
  ArrowUp: { code: "ArrowUp", vk: 38 },
  ArrowLeft: { code: "ArrowLeft", vk: 37 },
  ArrowRight: { code: "ArrowRight", vk: 39 },
  Home: { code: "Home", vk: 36 },
  End: { code: "End", vk: 35 },
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Browser actions on one tab. */
export class BrowserTab {
  private cdp: CdpSession;

  constructor(cdp: CdpSession) {
    this.cdp = cdp;
  }

  static async attach(wsUrl: string): Promise<BrowserTab> {
    const cdp = await CdpSession.connect(wsUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    return new BrowserTab(cdp);
  }

  get isOpen(): boolean {
    return this.cdp.isOpen;
  }

  close(): void {
    this.cdp.close();
  }

  async evaluate<T = unknown>(expression: string): Promise<T> {
    const result = await this.cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error("page_script_failed");
    return result.result?.value as T;
  }

  async snapshot(): Promise<PageSnapshot> {
    return JSON.parse(await this.evaluate<string>(SNAPSHOT)) as PageSnapshot;
  }

  async read(): Promise<string> {
    return formatSnapshot(await this.snapshot());
  }

  async open(url: string): Promise<string> {
    let target = url.trim();
    if (!/^https?:\/\//i.test(target)) target = `https://${target}`;
    const loaded = this.cdp.once("Page.loadEventFired", 20_000);
    const nav = await this.cdp.send("Page.navigate", { url: target });
    if (nav.errorText) throw new Error(`navigation_failed: ${nav.errorText}`);
    await loaded;
    await sleep(500);
    return this.read();
  }

  private async center(ref?: number, text?: string): Promise<{ x: number; y: number }> {
    const expr = `(() => {
      const ref = ${JSON.stringify(ref ?? null)}, text = ${JSON.stringify(text ?? null)};
      let el = ref != null ? document.querySelector('[data-heli-ref="' + ref + '"]') : null;
      if (!el && text) {
        const want = text.trim().toLowerCase();
        const all = [...document.querySelectorAll("[data-heli-ref], a, button, [role=button], input[type=submit], label")];
        el = all.find((e) => (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase() === want)
          || all.find((e) => (e.innerText || e.value || e.getAttribute("aria-label") || "").toLowerCase().includes(want));
      }
      if (!el) return null;
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
    })()`;
    const raw = await this.evaluate<string | null>(expr);
    if (!raw) throw new Error("element_not_found");
    return JSON.parse(raw);
  }

  async click(args: { ref?: number; text?: string; x?: number; y?: number }): Promise<string> {
    const point =
      typeof args.x === "number" && typeof args.y === "number" ? { x: args.x, y: args.y } : await this.center(args.ref, args.text);
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await this.cdp.send("Input.dispatchMouseEvent", { type, x: point.x, y: point.y, button: "left", clickCount: 1 });
    }
    await sleep(800);
    return this.read();
  }

  async type(args: { ref?: number; text: string; submit?: boolean }): Promise<string> {
    if (args.ref != null) {
      const ok = await this.evaluate<boolean>(`(() => {
        const el = document.querySelector('[data-heli-ref="${Number(args.ref)}"]');
        if (!el) return false;
        el.scrollIntoView({ block: "center" });
        el.focus();
        if ("select" in el && typeof el.select === "function") el.select();
        else if (el.isContentEditable) document.execCommand("selectAll");
        return true;
      })()`);
      if (!ok) throw new Error("element_not_found");
    }
    await this.cdp.send("Input.insertText", { text: args.text ?? "" });
    if (args.submit) await this.press("Enter");
    await sleep(args.submit ? 1200 : 200);
    // The typed value may be a password: never echo it back to the model.
    return args.submit ? this.read() : "Текст введён.";
  }

  async press(key: string): Promise<string> {
    const spec = KEYS[key] ?? { code: key, vk: 0 };
    const base = { key, code: spec.code, windowsVirtualKeyCode: spec.vk, nativeVirtualKeyCode: spec.vk };
    await this.cdp.send("Input.dispatchKeyEvent", { type: "keyDown", ...base, ...(spec.text ? { text: spec.text } : {}) });
    await this.cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
    await sleep(300);
    return `Нажата клавиша ${key}.`;
  }

  /** Search Yandex in this tab like a person. Throws "captcha" if Yandex asks for one and it is not solved in time. */
  async search(query: string, onCaptcha?: () => void, captchaWaitMs = 120_000): Promise<string> {
    await this.open(`https://ya.ru/search/?text=${encodeURIComponent(query)}&lr=213`);
    if (await this.isCaptcha()) {
      onCaptcha?.();
      const deadline = Date.now() + captchaWaitMs;
      while ((await this.isCaptcha()) && Date.now() < deadline) await sleep(2000);
      if (await this.isCaptcha()) throw new Error("captcha");
      await sleep(1000);
    }
    const hits = JSON.parse(await this.evaluate<string>(SERP)) as Array<{ title: string; url: string; snippet: string }>;
    if (!hits.length) return "Ничего не найдено.";
    return hits.map((h, i) => `${i + 1}. ${h.title}\n${h.url}\n${h.snippet}`).join("\n\n");
  }

  async isCaptcha(): Promise<boolean> {
    return this.evaluate<boolean>(CAPTCHA);
  }

  async screenshot(): Promise<string> {
    const shot = await this.cdp.send<{ data: string }>("Page.captureScreenshot", { format: "jpeg", quality: 60 });
    return shot.data;
  }
}
