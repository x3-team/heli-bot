"""Heli's own computer on the server: a headless Chromium per account, shown live in the app.

- The agent's browser_* tools and the ya.ru search run here (the app's local browser is not used).
- The user sees the latest frame in the «Компьютер агента» panel (GET /v1/cloud-browser/screen)
  and can click and type into it to sign in to a service or pass a captcha.
- Sign-ins persist: cookies and local storage are saved per account, encrypted with
  VAULT_MASTER_KEY, and loaded the next time that account's browser opens.

Playwright's sync API is bound to the thread that started it, so one worker thread owns the
browser and every call is queued to it. Requests to private and loopback addresses are blocked,
so a page cannot reach the API or other services on this host.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.web import WebError, check_url

log = logging.getLogger(__name__)

IDLE_S = 10 * 60
CALL_TIMEOUT_S = 60
CAPTCHA_WAIT_S = 120
VIEWPORT = {"width": 1280, "height": 800}
JPEG_QUALITY = 60

TOOL_NAMES = (
    "browser_open",
    "browser_read",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_screenshot",
)
KEYS = {
    "Enter", "Escape", "Tab", "Backspace", "Delete", "PageDown", "PageUp", "ArrowDown", "ArrowUp",
    "ArrowLeft", "ArrowRight", "Home", "End", "Space",
}


class CloudError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Result:
    text: str
    error: str | None = None
    frame: bytes | None = None


# Same snapshot as packages/ui/src/cdp.ts, so the model reads pages the same way everywhere.
SNAPSHOT = r"""() => {
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
    return t.replace(/\s+/g, " ").slice(0, 80);
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
  const text = (document.body ? document.body.innerText : "").replace(/\n{3,}/g, "\n\n").slice(0, MAX_TEXT);
  return JSON.stringify({ url: location.href, title: document.title, text, elements });
}"""

FIND = r"""([ref, text]) => {
  let el = ref != null ? document.querySelector('[data-heli-ref="' + ref + '"]') : null;
  if (!el && text) {
    const want = text.trim().toLowerCase();
    const all = [...document.querySelectorAll("[data-heli-ref], a, button, [role=button], input[type=submit], label")];
    const name = (e) => (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase();
    el = all.find((e) => name(e) === want) || all.find((e) => name(e).includes(want));
  }
  if (!el) return null;
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2];
}"""

CAPTCHA = r"""() => /showcaptcha|checkcaptcha|captcha/i.test(location.pathname)
  || /Подтвердите, что запросы отправляли вы|Я не робот|SmartCaptcha/i.test(document.body ? document.body.innerText.slice(0, 3000) : "")"""

# Organic results from a Yandex results page; skips ads and Yandex's own services. Same as cdp.ts.
SERP = r"""() => {
  const out = [], seen = new Set();
  const items = document.querySelectorAll("li.serp-item, .serp-item, [data-cid]");
  const pick = (root) => {
    if (root.querySelector(".label_theme_direct, [data-fast-name='direct'], .AdvLabel")) return;
    const a = root.querySelector("a.OrganicTitle-Link, a.organic__url, h2 a, a[href^='http']");
    if (!a) return;
    let href = a.href;
    try { const u = new URL(href); if (/(^|\.)yandex\.|(^|\.)ya\.ru$/.test(u.hostname)) return; } catch { return; }
    if (seen.has(href)) return;
    seen.add(href);
    const title = (root.querySelector("h2, .OrganicTitle-LinkText") || a).innerText.trim().replace(/\s+/g, " ");
    const snip = root.querySelector(".OrganicTextContentSpan, .organic__content-wrapper, .text-container, .Organic-ContentWrapper");
    out.push({ title: title.slice(0, 200), url: href, snippet: (snip ? snip.innerText : "").trim().replace(/\s+/g, " ").slice(0, 300) });
  };
  items.forEach((el) => out.length < 8 && pick(el));
  return JSON.stringify(out);
}"""


def format_snapshot(snap: dict) -> str:
    lines = [f"Адрес: {snap.get('url', '')}", f"Заголовок: {snap.get('title', '')}", ""]
    lines.append((snap.get("text") or "").strip() or "(текста нет)")
    elements = snap.get("elements") or []
    if elements:
        lines += ["", "Элементы:"]
        for el in elements:
            label = f" «{el['label']}»" if el.get("label") else ""
            lines.append(f"[{el['ref']}] {el['kind']}{label}")
    return "\n".join(lines)


def format_hits(hits: list[dict]) -> str:
    if not hits:
        return "Ничего не найдено."
    return "\n\n".join(f"{i}. {h['title']}\n{h['url']}\n{h.get('snippet', '')}" for i, h in enumerate(hits, 1))


def request_allowed(url: str, resolver=None) -> bool:
    """Pages may load anything public; nothing on this host or its private network."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in {"data", "blob", "about"}:
        return True
    if scheme not in {"http", "https", "ws", "wss"}:
        return False
    if scheme in {"ws", "wss"}:
        url = "http" + url[2:]
    try:
        check_url(url, **({"resolver": resolver} if resolver else {}))
    except WebError:
        return False
    return True


# --- saved sign-ins -------------------------------------------------------------------


def _key() -> bytes | None:
    raw = os.environ.get("VAULT_MASTER_KEY", "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error:
        return None
    return key if len(key) == 32 else None


class ProfileStore:
    """Cookies and local storage per account, AES-GCM sealed on disk. No key: nothing is kept."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().cloud_browser_profiles)

    def _path(self, user_id: uuid.UUID) -> Path:
        return self.root / f"{uuid.UUID(str(user_id))}.bin"

    def load(self, user_id: uuid.UUID) -> dict | None:
        key, path = _key(), self._path(user_id)
        if key is None or not path.exists():
            return None
        try:
            blob = path.read_bytes()
            return json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], str(user_id).encode()))
        except Exception:  # noqa: BLE001
            log.info("cloud browser profile unreadable, starting clean")
            return None

    def save(self, user_id: uuid.UUID, state: dict) -> bool:
        key = _key()
        if key is None:
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(key).encrypt(nonce, json.dumps(state).encode(), str(user_id).encode())
        path = self._path(user_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return True

    def forget(self, user_id: uuid.UUID) -> None:
        self._path(user_id).unlink(missing_ok=True)

    def exists(self, user_id: uuid.UUID) -> bool:
        return self._path(user_id).exists()


# --- one account's browser (worker thread only) -------------------------------------


class Page:
    def __init__(self, context, page) -> None:
        self.context = context
        self.page = page
        self.last_used = time.monotonic()

    def frame(self) -> bytes | None:
        try:
            return self.page.screenshot(type="jpeg", quality=JPEG_QUALITY, timeout=10_000)
        except Exception:  # noqa: BLE001
            return None

    def state(self) -> dict | None:
        try:
            return self.context.storage_state()
        except Exception:  # noqa: BLE001
            return None

    def location(self) -> tuple[str, str]:
        try:
            return self.page.url, self.page.title()
        except Exception:  # noqa: BLE001
            return "", ""

    def read(self) -> str:
        return format_snapshot(json.loads(self.page.evaluate(SNAPSHOT)))

    def goto(self, url: str) -> None:
        try:
            target = check_url(url)
        except WebError as exc:
            raise CloudError(exc.code) from None
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:  # noqa: BLE001
            raise CloudError("navigation_failed") from exc
        self.settle(1500)

    def open(self, url: str) -> str:
        self.goto(url)
        return self.read()

    def click(self, ref: int | None, text: str | None) -> str:
        point = self.page.evaluate(FIND, [ref, text])
        if not point:
            raise CloudError("element_not_found")
        self.page.mouse.click(point[0], point[1])
        self.settle(1200)
        return self.read()

    def type(self, ref: int | None, text: str, submit: bool) -> str:
        if ref is not None:
            handle = self.page.query_selector(f'[data-heli-ref="{int(ref)}"]')
            if handle is None:
                raise CloudError("element_not_found")
            handle.scroll_into_view_if_needed(timeout=5_000)
            handle.click(timeout=5_000)
            self.page.keyboard.press("Control+A")
        self.page.keyboard.insert_text(text)
        if submit:
            self.page.keyboard.press("Enter")
            self.settle(1500)
            return self.read()
        # The typed value may be a password: never echo it back to the model.
        return "Текст введён."

    def press(self, key: str) -> str:
        if key not in KEYS:
            raise CloudError("bad_key")
        self.page.keyboard.press(" " if key == "Space" else key)
        self.settle(400)
        return f"Нажата клавиша {key}."

    def is_captcha(self) -> bool:
        try:
            return bool(self.page.evaluate(CAPTCHA))
        except Exception:  # noqa: BLE001
            return False

    def serp(self) -> list[dict]:
        return json.loads(self.page.evaluate(SERP))

    # The user's own hands in the panel.
    def user_input(self, action: dict) -> None:
        kind = action.get("kind")
        if kind == "click":
            x = min(max(float(action.get("x", 0)), 0.0), 1.0) * VIEWPORT["width"]
            y = min(max(float(action.get("y", 0)), 0.0), 1.0) * VIEWPORT["height"]
            self.page.mouse.click(x, y)
            self.settle(600)
        elif kind == "text":
            self.page.keyboard.insert_text(str(action.get("text") or "")[:2000])
            self.page.wait_for_timeout(150)
        elif kind == "key":
            key = str(action.get("key") or "")
            if key not in KEYS:
                raise CloudError("bad_key")
            self.page.keyboard.press(" " if key == "Space" else key)
            self.settle(400)
        elif kind == "scroll":
            self.page.mouse.wheel(0, max(min(float(action.get("dy", 0)), 3000), -3000))
            self.page.wait_for_timeout(300)
        elif kind == "open":
            self.goto(str(action.get("url") or ""))
        elif kind == "back":
            try:
                self.page.go_back(wait_until="domcontentloaded", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
            self.settle(500)
        elif kind != "refresh":
            raise CloudError("bad_input")

    def settle(self, ms: int) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=ms + 3000)
        except Exception:  # noqa: BLE001
            pass
        self.page.wait_for_timeout(ms)

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:  # noqa: BLE001
            pass


def run_tool(page: Page, tool: str, args: dict) -> str:
    ref = args.get("ref") if isinstance(args.get("ref"), int) and not isinstance(args.get("ref"), bool) else None
    if tool == "browser_open":
        return page.open(str(args.get("url") or ""))
    if tool == "browser_read":
        return page.read()
    if tool == "browser_click":
        text = args.get("text") if isinstance(args.get("text"), str) else None
        if ref is None and not text:
            raise CloudError("element_not_found")
        return page.click(ref, text)
    if tool == "browser_type":
        return page.type(ref, str(args.get("text") or ""), args.get("submit") is True)
    if tool == "browser_press":
        return page.press(str(args.get("key") or "Enter"))
    if tool == "browser_screenshot":
        return "Снимок сделан."
    raise CloudError("unknown_tool")


def chromium_env(lib_root: str) -> dict:
    """Environment for Chromium: add the libraries unpacked without root, if there are any."""
    env = dict(os.environ)
    if lib_root:
        dirs = [str(Path(lib_root) / sub) for sub in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", "usr/lib")]
        dirs = [d for d in dirs if Path(d).is_dir()]
        if dirs:
            env["LD_LIBRARY_PATH"] = ":".join(dirs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    return env


def playwright_launcher():
    """Start Playwright and Chromium. Returns (new_page(storage_state) -> Page, shutdown())."""
    from playwright.sync_api import sync_playwright

    settings = get_settings()
    env = chromium_env(settings.cloud_browser_libs)
    if env.get("LD_LIBRARY_PATH"):
        # Playwright's driver checks host libraries with its own environment before launching:
        # it has to see the unpacked ones too.
        os.environ["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH"]
        os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
    pw = sync_playwright().start()
    options: dict = {
        "headless": True,
        "args": ["--disable-dev-shm-usage", "--no-first-run", "--disable-gpu", "--mute-audio"],
    }
    if settings.cloud_browser_executable:
        options["executable_path"] = settings.cloud_browser_executable
    options["env"] = env
    try:
        browser = pw.chromium.launch(**options)
    except Exception:
        pw.stop()
        raise

    def guard(route) -> None:
        if request_allowed(route.request.url):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def new_page(storage_state: dict | None) -> Page:
        context = browser.new_context(
            viewport=VIEWPORT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            accept_downloads=False,
            service_workers="block",
            storage_state=storage_state,
        )
        context.route("**/*", guard)
        page = context.new_page()
        page.set_default_timeout(20_000)
        return Page(context, page)

    def shutdown() -> None:
        try:
            browser.close()
        finally:
            pw.stop()

    return new_page, shutdown


@dataclass
class _Frame:
    jpeg: bytes
    version: int
    url: str = ""
    title: str = ""


@dataclass
class CloudBrowser:
    """One browser per account, all on one worker thread."""

    launcher: Callable[[], tuple] = playwright_launcher
    max_sessions: int = 2
    store: ProfileStore | None = None
    sessions: dict[uuid.UUID, Page] = field(default_factory=dict)
    frames: dict[uuid.UUID, _Frame] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._new_page = None
        self._shutdown = None
        if self.store is None:
            self.store = ProfileStore()

    # --- public, any thread ---------------------------------------------------

    def call(self, user_id: uuid.UUID, tool: str, args: dict) -> Result:
        """Run one browser_* tool for the agent."""
        return self._run(user_id, lambda page: run_tool(page, tool, args))

    def search(
        self,
        user_id: uuid.UUID,
        query: str,
        on_captcha: Callable[[bool], None] | None = None,
        halted: Callable[[], bool] | None = None,
        wait_s: float = CAPTCHA_WAIT_S,
        poll_s: float = 2.0,
    ) -> Result:
        """Search ya.ru like a person. If Yandex asks for a captcha, the user passes it in the panel."""
        url = f"https://ya.ru/search/?text={quote(query)}&lr=213"
        first = self._run(user_id, lambda page: (page.goto(url), "captcha" if page.is_captcha() else "")[1])
        if first.error:
            return first
        if first.text == "captcha":
            if on_captcha:
                on_captcha(True)
            deadline = time.monotonic() + wait_s
            solved = False
            while time.monotonic() < deadline and not (halted and halted()):
                time.sleep(poll_s)
                check = self._run(user_id, lambda page: "captcha" if page.is_captcha() else "")
                if check.error:
                    break
                if check.text != "captcha":
                    solved = True
                    break
            if on_captcha:
                on_captcha(False)
            if not solved:
                return Result("Ошибка: captcha", "captcha", self.frame(user_id))
        return self._run(user_id, lambda page: format_hits(page.serp()))

    def user_input(self, user_id: uuid.UUID, action: dict) -> Result:
        """A click or keystroke from the user in the panel."""
        return self._run(user_id, lambda page: (page.user_input(action), "ok")[1], save=True)

    def stop(self, user_id: uuid.UUID) -> None:
        if self._thread is not None:
            self._submit(lambda: self._close(user_id)).result(timeout=CALL_TIMEOUT_S)

    def forget(self, user_id: uuid.UUID) -> None:
        """Sign out everywhere: close the browser and delete the saved sign-ins."""
        self.stop(user_id)
        self.store.forget(user_id)

    def status(self, user_id: uuid.UUID) -> dict:
        item = self.frames.get(user_id)
        return {
            "running": user_id in self.sessions,
            "version": item.version if item else 0,
            "url": item.url if item else "",
            "title": item.title if item else "",
            "saved_logins": self.store.exists(user_id),
            "idle_minutes": IDLE_S // 60,
        }

    def frame(self, user_id: uuid.UUID) -> bytes | None:
        item = self.frames.get(user_id)
        return item.jpeg if item else None

    def frame_version(self, user_id: uuid.UUID) -> int:
        item = self.frames.get(user_id)
        return item.version if item else 0

    def active(self) -> int:
        return len(self.sessions)

    # --- worker thread ----------------------------------------------------------

    def _run(self, user_id: uuid.UUID, action: Callable[[Page], str], save: bool = True) -> Result:
        try:
            future = self._submit(lambda: self._do(user_id, action, save))
            return future.result(timeout=CALL_TIMEOUT_S)
        except CloudError as exc:
            return Result(f"Ошибка облачного браузера: {exc.code}", exc.code, self.frame(user_id))
        except FutureTimeout:
            return Result("Ошибка облачного браузера: timeout", "timeout")

    def _submit(self, job: Callable) -> Future:
        future: Future = Future()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="cloud-browser", daemon=True)
                self._thread.start()
        self._jobs.put((job, future))
        return future

    def _work(self) -> None:
        while True:
            try:
                job, future = self._jobs.get(timeout=30)
            except queue.Empty:
                self._reap()
                continue
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(job())
            except CloudError as exc:
                future.set_exception(exc)
            except BaseException as exc:  # noqa: BLE001
                log.info("cloud browser call failed: %s", type(exc).__name__)
                failed = CloudError("browser_failed")
                failed.__cause__ = exc
                future.set_exception(failed)
            self._reap()

    def _page(self, user_id: uuid.UUID) -> Page:
        page = self.sessions.get(user_id)
        if page is not None:
            return page
        if len(self.sessions) >= self.max_sessions:
            raise CloudError("browser_busy")
        if self._new_page is None:
            try:
                self._new_page, self._shutdown = self.launcher()
            except Exception as exc:  # noqa: BLE001
                log.info("cloud browser failed to start: %s", type(exc).__name__)
                raise CloudError("browser_unavailable") from None
        page = self._new_page(self.store.load(user_id))
        self.sessions[user_id] = page
        return page

    def _do(self, user_id: uuid.UUID, action: Callable[[Page], str], save: bool) -> Result:
        page = self._page(user_id)
        page.last_used = time.monotonic()
        try:
            text = action(page)
        except CloudError:
            self._capture(user_id, page)
            raise
        frame = self._capture(user_id, page)
        if save:
            self._save(user_id, page)
        return Result(text, None, frame)

    def _capture(self, user_id: uuid.UUID, page: Page) -> bytes | None:
        jpeg = page.frame()
        if jpeg:
            url, title = page.location()
            self.frames[user_id] = _Frame(jpeg, self.frame_version(user_id) + 1, url, title)
        return jpeg

    def _save(self, user_id: uuid.UUID, page: Page) -> None:
        state = page.state()
        if state is not None:
            try:
                self.store.save(user_id, state)
            except OSError:
                log.info("cloud browser profile save failed")

    def _close(self, user_id: uuid.UUID) -> None:
        page = self.sessions.pop(user_id, None)
        if page is not None:
            self._save(user_id, page)
            page.close()
        self._maybe_shutdown()

    def _reap(self) -> None:
        now = time.monotonic()
        for user_id, page in list(self.sessions.items()):
            if now - page.last_used > IDLE_S:
                self._close(user_id)
        self._maybe_shutdown()

    def _maybe_shutdown(self) -> None:
        # Nobody is using it: give the memory back to the host.
        if not self.sessions and self._shutdown is not None:
            try:
                self._shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._new_page = None
            self._shutdown = None


def ready() -> bool:
    """On, installed, and Chromium started at least once on this host."""
    if _ready:
        return available()
    if available() and time.monotonic() - _last_probe > 60:
        return _try_register()
    return False


def available() -> bool:
    if not get_settings().cloud_browser_enabled:
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_pool: CloudBrowser | None = None
_ready = False
_probe_error = ""
_last_probe = 0.0
_pool_lock = threading.Lock()


def pool() -> CloudBrowser:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = CloudBrowser(max_sessions=max(1, get_settings().cloud_browser_max))
        return _pool


def set_pool(value: CloudBrowser | None) -> None:
    """Tests swap in a fake launcher."""
    global _pool
    with _pool_lock:
        _pool = value


# --- the agent loop's executor ------------------------------------------------------

SEARCH_TOOL = "browser_search"
CAPTCHA_NOTICE = "Яндекс просит пройти капчу. Пройдите её на экране «Компьютер агента», и я продолжу."
_SHOT_TOOLS = {"browser_screenshot"}


def _run_for(task_id, user_id):
    try:
        from app.agent import runs

        return runs.registry.get(task_id, user_id)
    except Exception:  # noqa: BLE001
        return None


def execute(session, user, task_id, name: str, args: dict):
    """app.agent.executors contract: run(session, user, task_id, name, args) -> ClientResult."""
    from app.agent.control import ClientResult

    browser = pool()
    agent_run = _run_for(task_id, user.id)
    publish = agent_run.publish if agent_run else (lambda event: None)
    halted = (lambda: agent_run.control.stopped) if agent_run else (lambda: False)
    if name == SEARCH_TOOL:
        result = browser.search(
            user.id,
            str(args.get("query") or ""),
            on_captcha=lambda on: publish({"type": "notice", "text": CAPTCHA_NOTICE if on else ""}),
            halted=halted,
        )
    else:
        result = browser.call(user.id, name, args)
    publish({"type": "screen", "version": browser.frame_version(user.id)})
    shot = result.frame if (name in _SHOT_TOOLS or result.error) else None
    return ClientResult(result.text, result.error, shot)


def probe(browser: CloudBrowser) -> bool:
    """Start Chromium once and close it: missing system libraries show up here, not mid-task."""
    global _probe_error

    def job() -> bool:
        new_page, shutdown = browser.launcher()
        shutdown()
        return True

    try:
        browser._submit(job).result(timeout=CALL_TIMEOUT_S)
        _probe_error = ""
        return True
    except Exception as exc:  # noqa: BLE001
        cause = exc.__cause__ or exc
        _probe_error = f"{type(cause).__name__}: {str(cause)[:400]}"
        log.warning("cloud browser off: Chromium did not start (%s)", _probe_error)
        return False


def register(background: bool = False) -> bool:
    """Called at startup. Without Playwright, with the flag off or if Chromium cannot start,
    the agent has no browser tools (or the app's local browser when that is switched on).
    A failed start is retried at most once a minute when the panel asks for the status."""
    if not available():
        return False
    if background:
        threading.Thread(target=_try_register, name="cloud-browser-probe", daemon=True).start()
        return True
    return _try_register()


def _try_register() -> bool:
    global _ready, _last_probe
    from app.agent.executors import register_executor

    _last_probe = time.monotonic()
    if not probe(pool()):
        return False
    register_executor((*TOOL_NAMES, SEARCH_TOOL), execute)
    _ready = True
    log.info("cloud browser on: max %s sessions", pool().max_sessions)
    return True


def last_error() -> str:
    return _probe_error
