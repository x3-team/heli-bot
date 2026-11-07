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


ONEC = """<!doctype html><html><head><title>Бухгалтерия предприятия / 1С:Предприятие</title>
<style>.press,.themeCell,.gridLine{cursor:pointer;padding:4px}</style></head><body>
<div id="themesCell"><div id="themesCell_theme_0" class="themeCell" onmousedown="document.title='Раздел:Банк'">Банк и касса</div>
<div id="themesCell_theme_1" class="themeCell">Продажи</div></div>
<div class="window"><div class="openedItem">Путеводитель по демо-базе</div>
<div class="openedClose" onclick="this.parentElement.remove()">×</div></div>
<div class="grid"><div class="gridHead"><div class="gridBoxText">Наименование</div><div class="gridBoxText">ИНН</div></div>
<div class="gridBody"><div class="gridLine"><div class="gridBoxText">ООО Ромашка</div><div class="gridBoxText">7701234567</div></div>
<div class="gridLine"><div class="gridBoxText">ИП Иванов</div><div class="gridBoxText">500100732259</div></div></div></div>
<div class="press" id="form0_Провести" style="position:absolute;left:600px;top:500px;width:120px;height:30px">Провести и закрыть</div>
<input class="editInput" id="form0_Дата_i0" value="05.11.2026"><div id="form0_Дата#title_text">Дата</div>
</body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_sees_1c_web_client(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        page.set_content(ONEC)
        p = cb.Page(page.context, page)
        assert p.close_start_windows() == ["Путеводитель по демо-базе"]
        text = p.read()
        assert "«Банк и касса»" in text and "«Продажи»" in text
        assert "ООО Ромашка | 7701234567" in text and "Таблица" in text
        assert "поле «Дата» = «05.11.2026»" in text
        assert "Это 1С" in text
        assert "Путеводитель" not in text
        p.click(None, "Банк и касса")
        assert page.title() == "Раздел:Банк"
        with pytest.raises(cb.CloudError) as refused:
            p.click_at(650, 515, confirmed=False)
        assert refused.value.code.startswith("needs_confirmation:Провести")
        assert "Нажал в точке" in p.click_at(650, 515, confirmed=True)
        browser_.close()


def test_same_1c_base_and_links():
    assert cb.onec_link("https://h/b/ru_RU/#e1cib/list/Справочник.Контрагенты")
    assert cb.same_base("https://h/b/ru_RU/#e1cib/a", "https://h/b/ru_RU/#e1cib/list/b")
    assert not cb.same_base("https://h/b/ru_RU/", "https://h/c/ru_RU/#e1cib/x")


def test_cookie_import_formats():
    c = cb.normalize_cookie({"name": "BITRIX_SM_LOGIN", "value": "v", "domain": ".x.bitrix24.ru",
                             "expirationDate": 1893456000, "sameSite": "no_restriction", "secure": True})
    assert c["expires"] == 1893456000 and c["sameSite"] == "None" and c["path"] == "/"
    assert cb.normalize_cookie({"name": "a"}) is None


def test_imported_cookies_join_the_saved_profile(browser, tmp_path):
    user = uuid.uuid4()
    taken = browser.import_cookies(user, [{"name": "sid", "value": "1", "domain": "x.bitrix24.ru"}])
    assert taken == 1 and browser.saved_sites(user) == ["x.bitrix24.ru"]


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_sees_a_captcha_widget_in_a_frame():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        p = cb.Page(page.context, page)
        page.set_content('<p>Вход</p><iframe src="about:blank#smartcaptcha.yandexcloud.net/checkbox" width="300" height="100"></iframe>')
        assert p.is_captcha()
        page.set_content('<p>Форма</p><iframe src="about:blank#recaptcha/api2/anchor?size=invisible" width="256" height="60"></iframe>')
        assert not p.is_captcha()
        page.set_content('<p>Форма</p><iframe src="about:blank#smartcaptcha" width="300" height="100" style="display:none"></iframe>')
        assert not p.is_captcha()
        browser_.close()


def test_storage_state_origins_are_checked():
    assert cb.normalize_origin({"origin": "https://b24.example", "localStorage": [{"name": "a", "value": "1"}, {"x": 1}]}) == {
        "origin": "https://b24.example", "localStorage": [{"name": "a", "value": "1"}]
    }
    assert cb.normalize_origin({"origin": "http://b24.example", "localStorage": [{"name": "a", "value": "1"}]}) is None
    assert cb.normalize_origin({"origin": "https://b24.example", "localStorage": []}) is None


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_reports_dialogs_and_failed_saves():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        p = cb.Page(page.context, page)
        page.set_content(
            '<div>Форма</div><div class="cloudWindow" style="position:absolute;z-index:500;left:300px;top:200px;'
            'width:400px;height:200px;background:#fff">Выберите период<div class="press">Выбрать</div>'
            '<div class="press">Отмена</div></div>'
        )
        text = p.read()
        assert "Поверх страницы открыто окно «Выберите период»" in text and "«Отмена»" in text
        page.set_content(
            '<input aria-label="Контрагент"><button onclick="document.getElementById(\'e\').style.display=\'block\'">'
            'Сохранить</button><div id="e" class="field-error" style="display:none">Заполните поле «Контрагент»</div>'
        )
        assert "Запись НЕ сохранена" in p.click(None, "Сохранить")
        report = p.save_report("Сохранить")
        assert "Заполните поле «Контрагент»" in report and "НЕ сохранена" in report
        page.set_content("<p>Документ</p>")
        page.evaluate("history.replaceState(null, '', '#supply/edit?id=0f1e2d3c-1111-2222-3333-444455556666')")
        assert "id записи в адресе: 0f1e2d3c-1111-2222-3333-444455556666" in p.save_report("Сохранить")
        assert p.save_report("Отмена") == ""
        browser_.close()


ONEC_ERR = """<!doctype html><html><head><title>Управление торговлей / 1С:Предприятие</title>
<style>.press{cursor:pointer;padding:4px}.win{position:absolute;z-index:50;left:200px;top:150px;width:400px;height:160px;background:#eee}</style></head><body>
<div id="themesCell"><div class="themeCell">Склад и доставка</div></div>
<div class="win" id="go"><div class="title">Перейти по ссылке</div><input value="e1cib/app/Отчет.Нет">
<div class="press" onclick="document.getElementById('go').remove()">Отмена</div></div>
<div class="win" id="err" style="top:200px;z-index:60"><div>Неверный формат навигационной ссылки!</div>
<div class="press" onclick="document.getElementById('err').remove()">OK</div></div>
<div class="win" id="ver" style="top:300px;z-index:40"><div>Версия конфигурации 3.0.150 была выпущена для ознакомления</div>
<div class="press" onclick="document.getElementById('ver').remove()">Закрыть</div></div>
<div class="press" style="position:absolute;left:10px;top:500px">ОК</div>
</body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_closes_1c_link_errors_and_does_not_retry_the_link():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        p = cb.Page(page.context, page)
        opened: list[str] = []
        p.goto = lambda url: (opened.append(url), page.set_content(ONEC_ERR))
        link = "https://demo.example/trade/ru_RU/#e1cib/app/%D0%9E%D1%82%D1%87%D0%B5%D1%82.%D0%9D%D0%B5%D1%82"
        text = p.open(link)
        assert text.startswith("nav_link_failed=true: навигационная ссылка e1cib/app/Отчет.Нет не открылась")
        assert "Неверный формат навигационной ссылки" in text and "версии конфигурации" in text
        assert page.evaluate("() => ['go', 'err', 've'].map((id) => !!document.getElementById(id))") == [False, False, False]
        assert "Неверный формат" not in text.split("Адрес:", 1)[1]
        # The same link again (encoded the other way): no second trip, straight to the menu.
        again = p.open("https://demo.example/trade/ru_RU/#e1cib/app/Отчет.Нет")
        assert "уже не открылась" in again and len(opened) == 1
        # A plain 1С page has nothing to close; an «ОК» of the form itself is never clicked.
        page.set_content(ONEC_ERR.replace("Неверный формат навигационной ссылки!", "Склад").replace("Версия конфигурации", "Вид"))
        assert p.close_error_windows() == []
        browser_.close()


def test_1c_links_are_cleaned_for_go_to_link():
    assert cb.onec_clean_link("«https://h/b/ru_RU/#e1cib/list/%D0%A1%D0%BF.%D0%9A»,") == "https://h/b/ru_RU/#e1cib/list/Сп.К"
    assert cb.onec_link_key("https://h/b/ru_RU/#e1cib/list/Справочник.Контрагенты") == "e1cib/list/Справочник.Контрагенты"


ONEC_GO = """<!doctype html><html><head><title>Бухгалтерия / 1С:Предприятие</title></head><body>
<div id="themesCell">Банк</div>
<script>
document.addEventListener("keydown", (e) => {
  if (e.key === "F11" && e.shiftKey) {
    const i = document.createElement("input"); i.value = "e1cib/list/Справочник.Старое"; document.body.appendChild(i); i.focus();
    i.addEventListener("keydown", (k) => { if (k.key === "Enter") { document.title = "Перешёл:" + i.value; i.remove(); } });
  }
});
</script></body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_go_to_link_replaces_the_remembered_link():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        page.set_content(ONEC_GO)
        p = cb.Page(page.context, page)
        assert p.onec_go("https://h/b/ru_RU/#e1cib/list/%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA.%D0%9A")
        assert page.title() == "Перешёл:e1cib/list/Справочник.К"
        browser_.close()


# 1С web client as it is drawn on accounting.demo.1c.ru (8.3.27): windows are div.cloud[id$=win] with their caption
# in data-title, a modal one has a dark .surface right under it; a report is a set of frames with [y] rows of [x] cells.
ONEC_WINDOWS = """<!doctype html><html><head><title>Бухгалтерия предприятия, редакция 3.0 (1С:Предприятие)</title>
<style>.cloud{position:absolute;background:#fff}.surface{position:absolute;left:0;top:0;width:1280px;height:800px}
.press{display:inline-block;cursor:pointer;padding:4px}</style></head><body>
<div id="themesCell"><div class="themeCell">Продажи</div></div>
<div id="form1_Счет#title_text">Счет:</div><input class="editInput" id="form1_Счет_i0" value="">
<input class="editInput" id="form1_КонецПериода_i0" value="30.09.2026">
<span id="openedCell_cmd_close" title="Закрыть" style="position:absolute;left:5px;top:60px;width:20px;height:20px"
 onclick="document.title='tab closed'"></span>
<div class="surface surfaceBlack" id="modalSurface" style="z-index:9040"></div>
<div class="cloud" id="ps1win" style="z-index:9024;left:600px;top:340px;width:500px;height:300px">
 <div id="ps1headerTopLine"><div class="toplineBox" data-title="Путеводитель по демонстрационной базе"></div>
 <span id="ps1headerTopLine_cmd_CloseButton" title="Закрыть" onclick="document.getElementById('ps1win').remove()">x</span></div>
 Добро пожаловать!</div>
<div class="cloud" id="ps2win" style="z-index:9044;left:460px;top:170px;width:480px;height:260px">
 <div id="ps2headerTopLine"><div class="toplineBox" data-title="Рекомендуется обновить версию конфигурации"></div></div>
 <p>Обновите программу.</p>
 <a class="press" id="form2_КнопкаОсновная" style="position:absolute;left:380px;top:220px"
  onclick="document.getElementById('ps2win').remove();document.getElementById('modalSurface').remove()">Закрыть</a></div>
<div class="cloud" id="ps3win" style="z-index:9050;left:780px;top:330px;width:300px;height:200px">
 <div class="toplineBox" data-title="Подсказка"></div>Совет дня</div>
</body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_1c_modal_window_and_a_covered_button():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        page.set_content(ONEC_WINDOWS)
        p = cb.Page(page.context, page)
        text = p.read()
        # The modal notice is what the model sees; the form behind it is not offered for clicks.
        assert "Поверх страницы открыто окно «Рекомендуется обновить версию конфигурации»" in text
        assert "Поверх страницы открыто окно «Подсказка»" not in text
        assert "Текст окна (форма за ним скрыта" in text
        assert "«Счет»" not in text and "Продажи" not in text.split("Элементы:")[1]
        # «Закрыть» of the notice lies under another window: pressed by its own events, not the tab's «Закрыть».
        out = p.click(None, "Закрыть")
        assert page.evaluate("() => !!document.getElementById('ps2win')") is False
        assert page.title() != "tab closed"
        assert "Не нажал" not in out
        # After the click the guide (a start window, known by its caption) is closed too.
        assert "Закрыл стартовые окна 1С: «Путеводитель по демонстрационной базе»" in out
        assert page.evaluate("() => !!document.getElementById('ps1win')") is False
        text = p.read()
        assert "поле «Счет» (пусто)" in text and "поле «Конец периода» = «30.09.2026»" in text
        browser_.close()


ONEC_QUESTION = """<!doctype html><html><head><title>Бухгалтерия (1С:Предприятие)</title>
<style>.cloud{position:absolute;background:#fff}.surface{position:absolute;left:0;top:0;width:1280px;height:800px}
.press{display:inline-block;cursor:pointer;padding:4px}</style></head><body>
<div id="themesCell">Банк</div>
<a class="press" id="form5_ФормаЗаписать" style="position:absolute;left:300px;top:100px" onclick="document.title='saved'">Записать</a>
<div class="surface surfaceBlack" style="z-index:9000"></div>
<div class="cloud" id="ps4win" style="z-index:9004;left:400px;top:300px;width:400px;height:160px">
 <div id="ps4headerTopLine"><div class="toplineBox" data-title=""></div>
 <span id="ps4headerTopLine_cmd_CloseButton" title="Закрыть" style="display:inline-block;width:20px;height:20px"></span></div>
 <div>Данные были изменены. Сохранить изменения?</div>
 <div><a class="press" onclick="document.title='yes'">Да</a><a class="press" onclick="document.title='no'">Нет</a>
 <a class="press">Отмена</a></div></div>
</body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_1c_question_is_answered_and_the_form_behind_is_not_clicked():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        page.set_content(ONEC_QUESTION)
        p = cb.Page(page.context, page)
        text = p.read()
        assert "окно «Данные были изменены. Сохранить изменения?»" in text and "«Нет»" in text
        assert "кнопка «Закрыть окно»" in text and "Записать" not in text.split("Элементы:")[1]
        blocked = p.click(None, "Записать")
        assert blocked.startswith("Не нажал «Записать»") and page.title() != "saved"
        p.click(None, "Нет")
        assert page.title() == "no"
        browser_.close()


ONEC_FORM = """<!doctype html><html><head><title>Реализация товаров (1С:Предприятие)</title>
<style>.gridBox{position:absolute;top:0;height:29px}.gridLine{position:relative;height:29px}</style></head><body>
<div id="themesCell">Продажи</div>
<div id="form8_Контрагент#title_text">Контрагент:</div><input class="editInput" id="form8_Контрагент_i0" value="">
<div id="grid_form8_Товары" style="position:relative;width:900px">
 <div class="gridHead"><div class="gridLine" style="position:relative;height:29px">
  <div class="gridBox" colindex="0" style="left:0;width:40px">N</div>
  <div class="gridBox" colindex="1" style="left:40px;width:300px">Номенклатура</div>
  <div class="gridBox" colindex="3" style="left:340px;width:200px">Количество</div>
  <div class="gridBox" colindex="6" style="left:540px;width:120px">Цена</div></div></div>
 <div class="gridBody"><div class="gridLine" rowindex="0">
  <div class="gridBox" colindex="0" style="left:0;width:40px">1</div>
  <div class="gridBox" colindex="2" style="left:40px;width:300px" onclick="document.title='cell'">Конфеты</div>
  <div class="gridBox" colindex="4" style="left:340px;width:120px">5,000</div>
  <div class="gridBox" colindex="5" style="left:460px;width:80px">шт</div>
  <div class="gridBox" colindex="6" style="left:540px;width:120px"></div></div></div></div>
<div id="editDropDown" style="position:absolute;z-index:9007;left:300px;top:300px;width:300px">
 <ol class="eddList"><li>Конфетпром ООО (00-000026)</li><li>Создать: Конфет</li></ol></div>
<div class="moxelContainer" style="width:600px;height:200px">
 <iframe style="width:200px;height:200px" srcdoc='<div y="0"><div x="0">Контрагенты</div></div><div y="1"><div x="0">Магазин №23</div></div>'></iframe>
 <iframe style="width:380px;height:200px" srcdoc='<div y="0"><div x="0">Контрагенты</div><div x="1">Дебет</div><div x="2">Кредит</div></div><div y="1"><div x="1"></div><div x="2">40,00</div></div>'></iframe>
</div>
</body></html>"""


@pytest.mark.skipif(_chromium() is None, reason="Playwright Chromium not installed")
def test_real_chromium_1c_fields_table_part_variants_and_report():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(executable_path=_chromium())
        page = browser_.new_page(viewport=cb.VIEWPORT)
        page.set_content(ONEC_FORM)
        page.wait_for_timeout(300)
        p = cb.Page(page.context, page)
        text = p.read()
        assert "[1] вариант «Конфетпром ООО (00-000026)»" in text
        assert "вариант: создаст новый элемент «Создать: Конфет»" in text
        assert "поле «Контрагент» (пусто)" in text
        # Cells of a table part: each with its column (the header box over two body columns too) and a ref.
        row = next(line for line in text.splitlines() if "строка 1 —" in line)
        assert "Номенклатура: Конфеты" in row and "Количество: 5,000" in row and "Цена: " in row
        ref = int(row.split("Номенклатура:")[0].rsplit("[", 1)[1].split("]")[0])
        assert "] ячейка" not in text  # cells are listed in the table, not again among elements
        p.click(ref, None)
        assert page.title() == "cell"
        # The report frames put back together by row and column.
        assert "Контрагенты | Дебет | Кредит" in text and "Магазин №23 |  | 40,00" in text
        browser_.close()


def test_format_snapshot_shows_1c_values_windows_and_reports():
    text = cb.format_snapshot({
        "url": "https://h/b/ru_RU/", "title": "1С", "text": "Выбор периода", "onec": True,
        "dialog": {"title": "Выбор периода", "buttons": ["3 квартал"], "onec": True},
        "elements": [
            {"ref": 1, "kind": "поле", "label": "Счет", "value": ""},
            {"ref": 2, "kind": "поле", "label": "Период", "value": "01.07.2026"},
            {"ref": 3, "kind": "ячейка", "label": "Цена", "inline": True},
        ],
        "rows": ["[4] строка 1 — [3] Цена: "],
        "report": ["Счет | Дебет", "62 | 66 310,41"], "reportCut": True,
    })
    assert "Поверх страницы открыто окно «Выбор периода»" in text and "Текст окна (форма за ним скрыта" in text
    assert "[1] поле «Счет» (пусто)" in text and "[2] поле «Период» = «01.07.2026»" in text
    assert "[3] ячейка" not in text and "[3] Цена:" in text
    assert "62 | 66 310,41" in text and "Отчёт длиннее" in text


def test_1c_link_is_not_opened_by_a_reload_while_signed_in():
    """A reload ends the 1С session (1c-06): when «Перейти по ссылке» does not come up, say so instead."""

    class Stub:
        url = "https://demo.example/accounting/ru_RU/"

        def wait_for_timeout(self, ms):
            pass

    class OneC(cb.Page):
        def __init__(self, signed_in):  # noqa: D107
            self.page = Stub()
            self.signed_in = signed_in
            self.reloaded: list[str] = []

        def is_onec(self):
            return True

        def onec_wait_ready(self, seconds=15):
            return True

        def onec_cleanup(self):
            return ""

        def onec_go(self, url):
            return False

        def onec_signed_in(self):
            return self.signed_in

        def goto(self, url):
            self.reloaded.append(url)

        def settle(self, ms):
            pass

        def close_start_windows(self):
            return []

        def close_error_windows(self):
            return []

        def read(self):
            return "страница"

    link = "https://demo.example/accounting/ru_RU/#e1cib/list/Справочник.Контрагенты"
    page = OneC(signed_in=True)
    text = page.open(link)
    assert text.startswith("Навигационную ссылку не открыл") and "сбросило бы вход" in text
    assert page.reloaded == []
    # On the sign-in form there is nothing to lose: the base is loaded with the link as before.
    page = OneC(signed_in=False)
    assert "Перезагрузил базу 1С" in page.open(link) and len(page.reloaded) == 1
