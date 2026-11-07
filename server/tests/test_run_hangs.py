"""Hung runs: Stop does not wait for a stuck page or model call, a stuck browser frees its slot,
a silent run is stopped by the watchdog, and the step stream always ends."""

from __future__ import annotations

import base64
import itertools
import threading
import time
import uuid

import httpx
import pytest

from app import cloud_browser as cb
from app.agent import runs
from app.agent.control import Control, Stopped
from app.llm import client as llm_client

KEY = base64.b64encode(b"k" * 32).decode()


class HangingPage(cb.Page):
    """A page whose read never returns until the test lets it go (a 1С form that hangs the worker)."""

    def __init__(self, state=None) -> None:  # noqa: D107
        self.context = None
        self.page = None
        self.last_used = 0.0
        self.url = "https://accounting.demo.1c.ru/"
        self.hang = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def frame(self):
        return b"\xff\xd8jpeg"

    def state(self):
        return {"cookies": [], "origins": []}

    def location(self):
        return self.url, "1С"

    def read(self):
        if self.hang.is_set():
            self.release.wait(10)
        return "Адрес: " + self.url

    def take_downloads(self):
        return []

    def is_captcha(self):
        return False

    def close(self):
        self.closed = True


@pytest.fixture
def hanging(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    pages: list[HangingPage] = []

    def launcher():
        def new_page(state):
            page = HangingPage(state)
            pages.append(page)
            return page

        return new_page, lambda: None

    pool = cb.CloudBrowser(launcher=launcher, max_sessions=2, store=cb.ProfileStore(tmp_path))
    yield pool, pages
    for page in pages:
        page.release.set()


def _wait_busy(pool: cb.CloudBrowser) -> None:
    for _ in range(200):
        if pool._busy_since:
            return
        time.sleep(0.01)
    raise AssertionError("the worker never picked the job up")


def test_stopped_run_does_not_wait_out_a_hung_page(hanging):
    pool, pages = hanging
    user = uuid.uuid4()
    pool.call(user, "browser_read", {})
    pages[0].hang.set()
    stopped = threading.Event()
    threading.Timer(0.2, stopped.set).start()
    started = time.monotonic()
    result = pool.call(user, "browser_read", {}, halted=stopped.is_set)
    assert result.error == "stopped"
    assert time.monotonic() - started < 3  # not the minute of CALL_TIMEOUT_S


def test_stop_replaces_a_stuck_worker_at_once(hanging):
    pool, pages = hanging
    user, other = uuid.uuid4(), uuid.uuid4()
    pool.call(user, "browser_read", {})
    pages[0].hang.set()
    caller = threading.Thread(target=lambda: pool.call(user, "browser_read", {}, halted=lambda: False), daemon=True)
    caller.start()
    _wait_busy(pool)
    pool._busy_since -= cb.CALL_TIMEOUT_S + 1  # the page has held the worker longer than anyone waits
    started = time.monotonic()
    pool.stop(user)
    assert time.monotonic() - started < 1
    assert pool.sessions == {}  # the slot is free
    # The next account gets a browser from a fresh worker right away.
    assert pool.call(other, "browser_read", {}).error is None
    # The stuck worker closes what it owned once the page lets go.
    pages[0].release.set()
    for _ in range(200):
        if pages[0].closed:
            break
        time.sleep(0.02)
    assert pages[0].closed


def test_stop_of_a_healthy_browser_still_closes_it(hanging):
    pool, pages = hanging
    user = uuid.uuid4()
    pool.call(user, "browser_read", {})
    pool.stop(user)
    assert user not in pool.sessions and pages[0].closed


class _Run:
    def __init__(self) -> None:
        self.control = Control()
        self.events: list[dict] = []
        self.browser_timeouts = 0

    def publish(self, event: dict) -> None:
        self.events.append(event)


def test_browser_restarts_after_timeouts_in_a_row(hanging, monkeypatch):
    pool, _ = hanging
    cb.set_pool(pool)
    try:
        user = type("U", (), {"id": uuid.uuid4()})()
        run = _Run()
        monkeypatch.setattr(cb, "_run_for", lambda task_id, user_id: run)
        stops: list = []
        monkeypatch.setattr(pool, "stop", lambda user_id, wait_s=0: stops.append(user_id))
        monkeypatch.setattr(pool, "call", lambda *a, **kw: cb.Result(cb.TIMEOUT_TEXT, "tool_timeout"))
        first = cb.execute(None, user, uuid.uuid4(), "browser_click", {"text": "Войти"})
        assert first.error == "tool_timeout" and stops == []
        second = cb.execute(None, user, uuid.uuid4(), "browser_read", {})
        assert second.error == "browser_restarted" and "browser_open" in second.content
        assert stops == [user.id]
        assert any(e["type"] == "notice" and "завис" in e["text"] for e in run.events)
        # A good call resets the count.
        monkeypatch.setattr(pool, "call", lambda *a, **kw: cb.Result("ok", None))
        cb.execute(None, user, uuid.uuid4(), "browser_read", {})
        assert run.browser_timeouts == 0
    finally:
        cb.set_pool(None)


def test_executor_ends_the_run_when_stop_comes_during_a_browser_call(hanging, monkeypatch):
    pool, pages = hanging
    cb.set_pool(pool)
    try:
        user = type("U", (), {"id": uuid.uuid4()})()
        run = _Run()
        monkeypatch.setattr(cb, "_run_for", lambda task_id, user_id: run)
        cb.execute(None, user, uuid.uuid4(), "browser_read", {})
        pages[0].hang.set()
        threading.Timer(0.2, run.control.stop).start()
        with pytest.raises(Stopped) as stopped:
            cb.execute(None, user, uuid.uuid4(), "browser_read", {})
        assert stopped.value.reason == "stop"
    finally:
        cb.set_pool(None)


# --- the run and its stream ------------------------------------------------------------------


def _run(**kw) -> runs.AgentRun:
    return runs.AgentRun(task_id=uuid.uuid4(), user_id=uuid.uuid4(), **kw)


def test_stream_ends_soon_after_stop_even_if_the_run_hangs():
    run = _run(stop_grace=0.2)
    run.publish({"type": "started"})
    run.stop()
    started = time.monotonic()
    events = [e for e in run.stream(0, heartbeat=0.05) if e is not None]
    assert time.monotonic() - started < 2
    assert [e["type"] for e in events] == ["started", "done"]
    assert events[-1]["stopped"] == "stop" and events[-1]["forced"] is True and events[-1]["reply"]
    # The run thread lets go later: its own «done» goes nowhere, a reconnect replays the same end.
    run.publish({"type": "done", "reply": "поздно"})
    assert [e["type"] for e in run.stream(0, heartbeat=0.05) if e is not None] == ["started", "done"]


def test_stream_ends_with_the_real_done_when_the_run_lets_go_in_time():
    run = _run(stop_grace=5)
    run.publish({"type": "started"})
    run.stop()
    threading.Timer(0.1, lambda: run.publish({"type": "done", "reply": "Сделано: …", "stopped": "stop"})).start()
    events = [e for e in run.stream(0, heartbeat=0.05) if e is not None]
    assert events[-1]["reply"] == "Сделано: …" and "forced" not in events[-1]


def test_silent_run_is_stopped_by_the_watchdog():
    run = _run(stall_after=0.15, stop_grace=0.15)
    run.publish({"type": "started"})
    events = [e for e in run.stream(0, heartbeat=0.05) if e is not None]
    assert [e["type"] for e in events] == ["started", "notice", "done"]
    assert "продолжи" in events[1]["text"]
    assert events[-1]["stopped"] == "stalled"
    assert run.control.stopped and run.control.reason == "stalled"
    with pytest.raises(Stopped) as stopped:
        run.control.checkpoint()
    assert stopped.value.reason == "stalled"


def test_paused_or_waiting_run_is_not_a_hang():
    run = _run(stall_after=0.05)
    run.publish({"type": "started"})
    run.control.pause()
    beats = list(itertools.islice(run.stream(1, heartbeat=0.05), 4))
    assert beats == [None] * 4 and not run.control.stopped
    run.control.resume()
    waiter = threading.Thread(target=lambda: _wait_client(run.control), daemon=True)
    waiter.start()
    for _ in range(100):
        if run.control.waiting_client:
            break
        time.sleep(0.01)
    beats = list(itertools.islice(run.stream(1, heartbeat=0.05), 4))
    assert beats == [None] * 4 and not run.finished
    run.control.stop()
    waiter.join(2)


def _wait_client(control: Control) -> None:
    try:
        control.wait_result("call_1")
    except Stopped:
        pass


def test_heartbeat_does_not_hold_the_lock():
    run = _run()
    run.publish({"type": "started"})
    stream = run.stream(1, heartbeat=0.05)
    assert next(stream) is None  # the listener got a ping and went away without reading on
    writer = threading.Thread(target=lambda: run.publish({"type": "step"}), daemon=True)
    writer.start()
    writer.join(1)
    assert not writer.is_alive()


def test_no_second_run_while_a_stopped_one_has_not_let_go(monkeypatch):
    registry = runs.RunRegistry()
    release = threading.Event()
    calls: list = []

    def fake_execute(*a, **kw):
        calls.append(1)
        release.wait(5)
        raise Stopped("stop")

    monkeypatch.setattr(runs, "execute_task", fake_execute)

    class Session:
        def get(self, model, key):
            return object()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    task_id, user_id = uuid.uuid4(), uuid.uuid4()
    kwargs = dict(task_id=task_id, user_id=user_id, session_factory=Session, client=None,
                  client_tools=[], estimate_rub=None, autopause=False)
    first = registry.start(**kwargs)
    first.stop_grace = 0.05
    first.stop()
    list(first.stream(0, heartbeat=0.02))
    assert first.finished
    assert registry.start(**kwargs) is first  # its thread is still inside the hung call
    release.set()
    first.thread.join(2)
    assert len(calls) == 1


def test_model_calls_have_a_timeout(monkeypatch):
    monkeypatch.delenv("HELI_LLM_TIMEOUT_S", raising=False)
    default = llm_client.default_timeout()
    assert isinstance(default, httpx.Timeout) and default.read == llm_client.LLM_TIMEOUT_S
    monkeypatch.setenv("HELI_LLM_TIMEOUT_S", "60")
    assert llm_client.default_timeout().read == 60
    monkeypatch.setenv("HELI_LLM_TIMEOUT_S", "0")
    assert llm_client.default_timeout() is None
    monkeypatch.setenv("HELI_API_KEY", "k")
    monkeypatch.setenv("HELI_LLM_TIMEOUT_S", "42")
    assert llm_client.HeliClient.from_env()._client.timeout.read == 42
