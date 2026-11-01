"""Heli's own browser on the server: pool, saved sign-ins, search captcha, executor, API."""

from __future__ import annotations

import base64
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import cloud_browser as cb
from app.agent.control import ClientResult

KEY = base64.b64encode(b"k" * 32).decode()


class FakePage(cb.Page):
    """Stands in for a Playwright page; records what the agent did."""

    def __init__(self, state=None) -> None:  # noqa: D107
        self.context = None
        self.page = None
        self.last_used = 0.0
        self.loaded_state = state
        self.url = "about:blank"
        self.done: list = []
        self.captcha = False
        self.closed = False
        self.mail_hits: list | None = None
        self.mail_queries: list[str] = []
        self.pumps = 0

    def frame(self):
        return b"\xff\xd8jpeg" + self.url.encode()

    def state(self):
        return {"cookies": [{"name": "sid", "value": "1", "domain": ".amocrm.ru"}], "origins": []}

    def location(self):
        return self.url, "Заголовок"

    def read(self):
        return f"Адрес: {self.url}\nЗаголовок: Заголовок\n\nтекст"

    def goto(self, url):
        if "10.0.0.1" in url:
            raise cb.CloudError("blocked_address")
        self.url = url

    def click(self, ref, text):
        self.done.append(("click", ref, text))
        return self.read()

    def type(self, ref, text, submit):
        self.done.append(("type", ref, text, submit))
        return "Текст введён."

    def press(self, key):
        self.done.append(("press", key))
        return f"Нажата клавиша {key}."

    def is_captcha(self):
        return self.captcha

    def serp(self):
        return [{"title": "amoCRM", "url": "https://www.amocrm.ru/", "snippet": "CRM"}]

    def mail_search(self, query):
        self.mail_queries.append(query)
        self.url = "https://go.mail.ru/search?q=" + query
        return self.mail_hits

    def pump(self):
        self.pumps += 1

    def user_input(self, action):
        self.done.append(("user", action))
        if action.get("kind") == "open":
            self.goto(action["url"])
        if action.get("kind") == "click":
            self.captcha = False

    def close(self):
        self.closed = True


class FakeLauncher:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.started = 0
        self.stopped = 0

    def __call__(self):
        self.started += 1

        def new_page(state):
            page = FakePage(state)
            self.pages.append(page)
            return page

        def shutdown():
            self.stopped += 1

        return new_page, shutdown


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    launcher = FakeLauncher()
    pool = cb.CloudBrowser(launcher=launcher, max_sessions=2, store=cb.ProfileStore(tmp_path))
    pool.launcher_ref = launcher
    return pool


def test_tools_run_in_one_browser_per_account(browser):
    user = uuid.uuid4()
    opened = browser.call(user, "browser_open", {"url": "https://amocrm.ru"})
    assert opened.error is None
    assert opened.text.startswith("Адрес: https://amocrm.ru")
    assert opened.frame and opened.frame.startswith(b"\xff\xd8")
    browser.call(user, "browser_click", {"ref": 3})
    browser.call(user, "browser_type", {"ref": 4, "text": "secret", "submit": False})
    launcher = browser.launcher_ref
    assert len(launcher.pages) == 1
    assert launcher.pages[0].done == [("click", 3, None), ("type", 4, "secret", False)]
    assert browser.status(user)["running"] is True
    assert browser.frame_version(user) == 3


def test_click_needs_ref_or_text(browser):
    result = browser.call(uuid.uuid4(), "browser_click", {"ref": True})
    assert result.error == "element_not_found"


def test_private_addresses_are_refused(browser):
    result = browser.call(uuid.uuid4(), "browser_open", {"url": "http://10.0.0.1/"})
    assert result.error == "blocked_address"


def test_pool_limit(browser):
    browser.call(uuid.uuid4(), "browser_read", {})
    browser.call(uuid.uuid4(), "browser_read", {})
    third = browser.call(uuid.uuid4(), "browser_read", {})
    assert third.error == "browser_busy"


def test_idle_browser_gives_its_place_to_the_next_account(browser, monkeypatch):
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    browser.call(a, "browser_open", {"url": "https://amocrm.ru"})
    browser.call(b, "browser_read", {})
    browser.sessions[a].last_used -= cb.EVICT_IDLE_S + 1
    third = browser.call(c, "browser_read", {})
    assert third.error is None
    assert set(browser.sessions) == {b, c}
    # a's sign-ins were saved before its browser closed
    assert browser.store.load(a)["cookies"][0]["value"] == "1"


def test_watched_browser_is_not_evicted(browser):
    a, b = uuid.uuid4(), uuid.uuid4()
    browser.call(a, "browser_read", {})
    browser.call(b, "browser_read", {})
    browser.sessions[a].last_used -= cb.EVICT_IDLE_S + 1
    browser._watched[a] = cb.time.monotonic()
    assert browser.call(uuid.uuid4(), "browser_read", {}).error == "browser_busy"


def test_agent_waits_in_line_for_a_free_browser(browser):
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    browser.call(a, "browser_read", {})
    browser.call(b, "browser_read", {})
    notices: list[bool] = []

    def on_wait(on: bool) -> None:
        notices.append(on)
        if on:
            threading.Timer(0.05, lambda: browser.stop(a)).start()

    assert browser.reserve(c, wait_s=5, on_wait=on_wait, poll_s=0.02) is None
    assert notices == [True, False]
    assert c in browser.sessions


def test_waiting_in_line_ends_on_stop_or_timeout(browser):
    browser.call(uuid.uuid4(), "browser_read", {})
    browser.call(uuid.uuid4(), "browser_read", {})
    assert browser.reserve(uuid.uuid4(), wait_s=0.05, poll_s=0.01) == "browser_busy"
    assert browser.reserve(uuid.uuid4(), wait_s=5, halted=lambda: True, poll_s=0.01) == "browser_busy"


def test_sign_ins_are_saved_encrypted_and_restored(browser, tmp_path):
    user = uuid.uuid4()
    browser.call(user, "browser_open", {"url": "https://amocrm.ru"})
    browser.stop(user)
    saved = (tmp_path / f"{user}.bin").read_bytes()
    assert b"sid" not in saved and b"amocrm" not in saved
    assert oct(os.stat(tmp_path / f"{user}.bin").st_mode & 0o777) == "0o600"
    assert browser.launcher_ref.stopped == 1  # nobody left: Chromium closed

    browser.call(user, "browser_read", {})
    restored = browser.launcher_ref.pages[-1].loaded_state
    assert restored["cookies"][0]["value"] == "1"

    browser.forget(user)
    assert not (tmp_path / f"{user}.bin").exists()
    assert browser.status(user)["saved_logins"] is False


def test_profile_of_another_account_does_not_open(browser, tmp_path):
    a, b = uuid.uuid4(), uuid.uuid4()
    browser.call(a, "browser_read", {})
    browser.stop(a)
    (tmp_path / f"{b}.bin").write_bytes((tmp_path / f"{a}.bin").read_bytes())
    assert browser.store.load(b) is None


def test_without_vault_key_nothing_is_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    store = cb.ProfileStore(tmp_path)
    assert store.save(uuid.uuid4(), {"cookies": []}) is False
    assert list(tmp_path.iterdir()) == []


def _captcha_page(browser, user):
    browser.call(user, "browser_read", {})
    page = browser.launcher_ref.pages[0]
    page.captcha = True
    return page


def test_search_uses_ya_ru_when_there_is_no_captcha(browser):
    user = uuid.uuid4()
    result = browser.search(user, "amoCRM цены")
    page = browser.launcher_ref.pages[0]
    assert result.error is None and "https://www.amocrm.ru/" in result.text
    assert page.url.startswith("https://ya.ru/search/?text=amoCRM%20")
    assert page.mail_queries == []


def test_search_goes_through_mail_ru_when_yandex_asks_for_a_captcha(browser):
    user = uuid.uuid4()
    page = _captcha_page(browser, user)
    page.mail_hits = [{"title": "GPU по часам", "url": "https://gpuindex.ru/", "snippet": "от 9 ₽/час"}]
    asked: list[bool] = []
    result = browser.search(user, "аренда GPU", ask_user=lambda: asked.append(True) or True)
    assert result.error is None and "https://gpuindex.ru/" in result.text
    assert asked == []  # the user is not bothered while the fallback works
    # The next search skips ya.ru: no captcha flash on the live screen.
    page.captcha = False
    page.url = "about:blank"
    browser.search(user, "ещё запрос")
    assert page.mail_queries == ["аренда GPU", "ещё запрос"]
    assert page.url.startswith("https://go.mail.ru/")


def test_search_asks_the_user_when_the_fallback_fails_too(browser):
    user = uuid.uuid4()
    page = _captcha_page(browser, user)

    def user_passes() -> bool:
        page.captcha = False
        return True

    result = browser.search(user, "amoCRM", ask_user=user_passes)
    assert result.error is None and "https://www.amocrm.ru/" in result.text
    assert page.url.startswith("https://ya.ru/search/")


def test_search_gives_up_on_unsolved_captcha(browser):
    user = uuid.uuid4()
    _captcha_page(browser, user)
    assert browser.search(user, "q", ask_user=lambda: False).error == "captcha"
    assert browser.search(user, "q").error == "captcha"


class _Run:
    def __init__(self) -> None:
        from app.agent.control import Control

        self.control = Control()
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def test_captcha_pauses_the_task_and_goes_on_when_the_user_passes_it(browser):
    user = uuid.uuid4()
    _captcha_page(browser, user)
    run = _Run()
    threading.Timer(0.05, lambda: browser.user_input(user, {"kind": "click", "x": 0.5, "y": 0.5})).start()
    assert cb.ask_for_captcha(browser, user, run, poll_s=0.02, wait_s=5) is True
    assert [e["type"] for e in run.events] == ["paused", "resumed"]
    assert run.events[0]["reason"] == "captcha" and "капчу" in run.events[0]["text"]
    assert run.control.paused is False


def test_captcha_wait_ends_on_stop_or_continue(browser):
    user = uuid.uuid4()
    _captcha_page(browser, user)
    run = _Run()
    threading.Timer(0.05, run.control.stop).start()
    assert cb.ask_for_captcha(browser, user, run, poll_s=0.02, wait_s=5) is False
    # «Продолжить» while the captcha is still there: go on at once, the agent is told it failed.
    run = _Run()
    threading.Timer(0.05, run.control.resume).start()
    assert cb.ask_for_captcha(browser, user, run, poll_s=0.02, wait_s=5) is False
    assert run.events[-1] == {"type": "resumed"}
    assert cb.ask_for_captcha(browser, user, None) is False


def test_open_that_lands_on_a_captcha_waits_for_the_user(browser, monkeypatch):
    cb.set_pool(browser)
    try:
        user = type("U", (), {"id": uuid.uuid4()})()
        run = _Run()
        monkeypatch.setattr(cb, "_run_for", lambda task_id, user_id: run)
        monkeypatch.setattr(cb, "CAPTCHA_POLL_S", 0.02)
        browser.call(user.id, "browser_read", {})
        page = browser.launcher_ref.pages[0]
        page.captcha = True
        threading.Timer(0.1, lambda: browser.user_input(user.id, {"kind": "click", "x": 0.5, "y": 0.5})).start()
        result = cb.execute(None, user, uuid.uuid4(), "browser_open", {"url": "https://avito.ru"})
        assert result.error is None
        assert result.content.startswith("Пользователь прошёл капчу.")
        assert "paused" in [e["type"] for e in run.events]
    finally:
        cb.set_pool(None)


def test_idle_worker_keeps_pages_alive_and_screens_fresh(browser, monkeypatch):
    monkeypatch.setattr(cb, "LIVE_FRAME_S", 0.0)
    user = uuid.uuid4()
    browser.call(user, "browser_read", {})
    page = browser.launcher_ref.pages[0]
    version = browser.frame_version(user)
    import time as _time

    _time.sleep(0.6)
    assert page.pumps > 0  # route handlers get to run between the agent's steps
    assert browser.frame_version(user) == version  # nobody watches, nothing new: no frames
    browser.watch(user)
    page.url = "https://ya.ru/showcaptcha"  # the page changed on its own (an image loaded)
    _time.sleep(0.6)
    assert browser.frame_version(user) == version + 1  # one new frame, not one per tick


def test_request_guard_cache(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cb, "_guard_cache", {})
    monkeypatch.setattr(cb, "request_allowed", lambda url, resolver=None: calls.append(url) or True)
    assert cb.request_allowed_cached("https://ya.ru/a.js")
    assert cb.request_allowed_cached("https://ya.ru/b.png")
    assert cb.request_allowed_cached("data:image/png;base64,AA")
    assert calls == ["https://ya.ru/a.js"]


def test_browser_that_cannot_start(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("no chromium")

    pool = cb.CloudBrowser(launcher=broken, store=cb.ProfileStore(tmp_path))
    assert pool.call(uuid.uuid4(), "browser_read", {}).error == "browser_unavailable"
    assert cb.probe(pool) is False
    assert cb.probe(cb.CloudBrowser(launcher=FakeLauncher(), store=cb.ProfileStore(tmp_path))) is True


def test_chromium_env_adds_unpacked_libraries(tmp_path):
    assert "LD_LIBRARY_PATH" not in cb.chromium_env("") or cb.chromium_env("") == dict(os.environ)
    (tmp_path / "usr/lib/x86_64-linux-gnu").mkdir(parents=True)
    env = cb.chromium_env(str(tmp_path))
    assert env["LD_LIBRARY_PATH"].startswith(str(tmp_path / "usr/lib/x86_64-linux-gnu"))


def test_request_guard():
    def resolver(host, port):
        table = {"example.com": "93.184.216.34", "evil.test": "127.0.0.1"}
        return [(None, None, None, None, (table[host], port))]

    assert cb.request_allowed("https://example.com/a.js", resolver)
    assert not cb.request_allowed("http://evil.test/", resolver)
    assert not cb.request_allowed("file:///etc/passwd", resolver)
    assert not cb.request_allowed("http://localhost:8001/v1/me", resolver)
    assert cb.request_allowed("data:image/png;base64,AA", resolver)


def test_executor_publishes_screen_and_keeps_shots_small(browser, monkeypatch):
    cb.set_pool(browser)
    try:
        user = type("U", (), {"id": uuid.uuid4()})()
        events: list[dict] = []
        run = type("R", (), {"publish": staticmethod(events.append), "control": type("C", (), {"stopped": False})()})()
        monkeypatch.setattr(cb, "_run_for", lambda task_id, user_id: run)
        opened = cb.execute(None, user, uuid.uuid4(), "browser_open", {"url": "https://amocrm.ru"})
        assert isinstance(opened, ClientResult)
        assert opened.screenshot is None  # live screen goes through /screen, not the trajectory
        shot = cb.execute(None, user, uuid.uuid4(), "browser_screenshot", {})
        assert shot.screenshot
        assert events[-1] == {"type": "screen", "version": 2}
    finally:
        cb.set_pool(None)


def test_register_only_when_enabled(monkeypatch):
    from app.agent import executors

    monkeypatch.setattr(cb, "available", lambda: False)
    assert cb.register() is False
    monkeypatch.setattr(cb, "available", lambda: True)
    monkeypatch.setattr(cb, "probe", lambda pool: False)
    assert cb.register() is False
    monkeypatch.setattr(cb, "probe", lambda pool: True)
    try:
        assert cb.register() is True
        assert executors.executor_for("browser_open") is cb.execute
        assert executors.executor_for("browser_search") is cb.execute
    finally:
        executors.unregister_executor((*cb.TOOL_NAMES, cb.SEARCH_TOOL, cb.UPLOAD_TOOL))
        cb._ready = False


# --- real Chromium, when it is installed ---------------------------------------------

PAGE = """<!doctype html><html><head><title>Тест</title></head><body>
<h1>Вход</h1><input id="login" placeholder="Логин"><button onclick="document.title='Нажато:'+document.getElementById('login').value">Войти</button>
<a href="/two">Дальше</a></body></html>"""

LATE = """<!doctype html><html><head><title>Жду</title></head><body>
<script>setTimeout(() => fetch("/data").then((r) => r.text()).then((t) => { document.title = t; }), 2000)</script>
</body></html>"""


def _chromium() -> str | None:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    for path in ("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome",):
        if os.path.exists(path):
            return path
    return os.environ.get("CLOUD_BROWSER_EXECUTABLE") or None


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_round_trip(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/late":
                body = LATE.encode()
            elif self.path == "/data":
                body = "Загружено".encode()
            else:
                body = PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("set-cookie", "sid=42; Path=/; Max-Age=3600")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("CLOUD_BROWSER_EXECUTABLE", _chromium())
    # The guard blocks loopback; this test serves its page from loopback.
    monkeypatch.setattr(cb, "check_url", lambda u, **kw: u)
    monkeypatch.setattr(cb, "request_allowed", lambda u, resolver=None: True)
    pool = cb.CloudBrowser(store=cb.ProfileStore(tmp_path))
    user = uuid.uuid4()
    try:
        opened = pool.call(user, "browser_open", {"url": url})
        assert opened.error is None, opened.text
        assert "Заголовок: Тест" in opened.text
        assert "[1] поле «Логин»" in opened.text
        assert opened.frame and opened.frame[:2] == b"\xff\xd8"
        assert pool.call(user, "browser_type", {"ref": 1, "text": "vitalii"}).text == "Текст введён."
        clicked = pool.call(user, "browser_click", {"text": "Войти"})
        assert "Заголовок: Нажато:vitalii" in clicked.text
        assert pool.user_input(user, {"kind": "click", "x": 0.01, "y": 0.01}).error is None
        # A request the page makes on its own between steps (a captcha image, a form the user
        # submits) must not hang until the agent's next call; the watched screen follows it.
        pool.call(user, "browser_open", {"url": url + "late"})
        pool.watch(user)
        import time as _time

        deadline = _time.monotonic() + 6
        while _time.monotonic() < deadline and pool.status(user)["title"] != "Загружено":
            _time.sleep(0.1)
        assert pool.status(user)["title"] == "Загружено"
        pool.stop(user)
        assert pool.store.load(user)["cookies"][0]["value"] == "42"
    finally:
        pool.stop(user)
        server.shutdown()


def _token(client, monkeypatch) -> str:
    monkeypatch.setenv("HELI_OPEN_ACCESS", "1")
    return client.post("/v1/auth/open").json()["access_token"]


def test_api_live_screen_and_user_input(client, browser, monkeypatch):
    headers = {"authorization": f"Bearer {_token(client, monkeypatch)}"}
    monkeypatch.setattr(cb, "ready", lambda: False)
    assert client.get("/v1/cloud-browser", headers=headers).json()["enabled"] is False
    assert client.post("/v1/cloud-browser/input", json={"kind": "refresh"}, headers=headers).status_code == 503

    monkeypatch.setattr(cb, "ready", lambda: True)
    cb.set_pool(browser)
    try:
        assert client.get("/v1/cloud-browser/screen", headers=headers).status_code == 404
        opened = client.post("/v1/cloud-browser/input", json={"kind": "open", "url": "https://amocrm.ru"}, headers=headers)
        assert opened.status_code == 200, opened.text
        assert opened.json()["running"] is True
        assert opened.json()["url"] == "https://amocrm.ru"
        screen = client.get("/v1/cloud-browser/screen", headers=headers)
        assert screen.headers["content-type"] == "image/jpeg"
        assert screen.headers["x-screen-version"] == "1"
        clicked = client.post("/v1/cloud-browser/input", json={"kind": "click", "x": 0.5, "y": 0.25}, headers=headers)
        assert clicked.json()["version"] == 2
        assert client.post("/v1/cloud-browser/input", json={"kind": "click", "x": 2}, headers=headers).status_code == 422
        assert client.post("/v1/cloud-browser/input", json={"kind": "eval"}, headers=headers).status_code == 422
        blocked = client.post("/v1/cloud-browser/input", json={"kind": "open", "url": "http://10.0.0.1"}, headers=headers)
        assert blocked.status_code == 400
        assert client.get("/v1/cloud-browser", headers=headers).json()["saved_logins"] is True
        forgot = client.post("/v1/cloud-browser/forget", headers=headers).json()
        assert forgot["saved_logins"] is False and forgot["running"] is False
        assert client.get("/v1/cloud-browser").status_code == 401
    finally:
        cb.set_pool(None)
