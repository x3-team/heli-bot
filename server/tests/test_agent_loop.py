import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import api
from app.agent import runs
from app.agent.control import ClientResult, Control, Stopped
from app.agent.loop import Budget, ToolResult, run_agent
from app.api import get_heli_client
from app.auth import issue_token
from app.llm.client import Completion, HeliClient, ToolCall
from app.models import TaskStep, User
from app.trajectories import start_task


def _completion(text="", calls=None, cost="1"):
    return Completion(
        alias="heli-fast",
        text=text,
        input_tokens=10,
        output_tokens=4,
        cost_rub=Decimal(cost),
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a)) for i, (n, a) in enumerate(calls or [])],
    )


def _scripted(*completions):
    queue = list(completions)
    seen = []

    def complete(messages, tools):
        seen.append((list(messages), tools))
        return queue.pop(0)

    complete.seen = seen
    return complete


TOOLS = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]


def test_loop_feeds_tool_results_and_answers() -> None:
    complete = _scripted(_completion(calls=[("web_search", {"query": "amo"})]), _completion("Готово"))
    events = []
    out = run_agent(
        [{"role": "user", "content": "x"}],
        tools=TOOLS,
        call_tool=lambda cid, name, args: ToolResult(f"нашёл {args['query']}"),
        complete=complete,
        budget=Budget(),
        on_event=events.append,
    )
    assert out.final_answer == "Готово" and out.stopped is None and out.steps == 2
    assert out.cost_rub == Decimal("2")
    assert [e["kind"] for e in events] == ["model", "tool_call", "tool_result", "answer"]
    assert complete.seen[1][0][-1] == {"role": "tool", "tool_call_id": "c0", "content": "нашёл amo"}


def test_rejected_confirmation_stops_the_run() -> None:
    complete = _scripted(
        _completion(calls=[("ask_confirmation", {"action": "отправить счёт"}), ("web_search", {"query": "q"})])
    )
    called = []
    out = run_agent(
        [],
        tools=TOOLS,
        call_tool=lambda cid, name, args: called.append(name) or ToolResult("rejected"),
        complete=complete,
        budget=Budget(),
    )
    assert out.stopped == "confirm"
    assert called == ["ask_confirmation"]


def test_autopause_without_a_live_client_ends_with_pause() -> None:
    complete = _scripted(*[_completion(calls=[("web_search", {"query": "q"})], cost="1") for _ in range(5)])
    out = run_agent(
        [],
        tools=TOOLS,
        call_tool=lambda *a: ToolResult("ok"),
        complete=complete,
        budget=Budget(estimate_rub=Decimal("1")),
    )
    # 1 ₽ spent is within 1.5 ₽; after the second call 2 ₽ > 1.5 ₽ → pause.
    assert out.stopped == "pause" and out.steps == 2


def test_autopause_off_keeps_going_until_budget() -> None:
    complete = _scripted(*[_completion(calls=[("web_search", {"query": "q"})], cost="1") for _ in range(5)])
    out = run_agent(
        [],
        tools=TOOLS,
        call_tool=lambda *a: ToolResult("ok"),
        complete=complete,
        budget=Budget(estimate_rub=Decimal("1"), autopause=False, max_rub=Decimal("3")),
    )
    assert out.stopped == "budget" and out.steps == 3


def test_autopause_waits_for_resume_then_continues() -> None:
    control = Control()
    paused = threading.Event()
    control.on_pause = lambda reason, details: paused.set()
    complete = _scripted(
        _completion(calls=[("web_search", {"query": "q"})], cost="2"), _completion("Итог", cost="1")
    )
    result = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "out",
            run_agent(
                [],
                tools=TOOLS,
                call_tool=lambda *a: ToolResult("ok"),
                complete=complete,
                budget=Budget(estimate_rub=Decimal("1")),
                control=control,
            ),
        )
    )
    worker.start()
    assert paused.wait(2)
    assert worker.is_alive()
    control.resume()
    worker.join(2)
    assert result["out"].final_answer == "Итог" and result["out"].stopped is None


def test_stop_interrupts_waiting_for_the_client() -> None:
    control = Control()
    started = threading.Event()

    def call_tool(cid, name, args):
        started.set()
        return ToolResult(control.wait_result(cid).content)

    complete = _scripted(_completion(calls=[("browser_open", {"url": "https://amocrm.ru"})]))
    result = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "out",
            run_agent([], tools=TOOLS, call_tool=call_tool, complete=complete, budget=Budget(), control=control),
        )
    )
    worker.start()
    assert started.wait(2)
    t0 = time.monotonic()
    control.stop()
    worker.join(2)
    assert time.monotonic() - t0 < 1
    assert result["out"].stopped == "stop"


def test_user_pause_blocks_before_the_next_step() -> None:
    control = Control()
    control.pause()
    with pytest.raises(Stopped):
        control.pause_timeout = 0.05
        control.checkpoint()


def test_client_timeout() -> None:
    control = Control(client_timeout=0.05)
    with pytest.raises(Stopped) as caught:
        control.wait_result("x")
    assert caught.value.reason == "client_timeout"
    assert control.deliver("x", ClientResult("late")) is False


# --- API: SSE stream with a client tool ----------------------------------------------------


class _BrowserScript:
    """classify → browser_open (client tool) → answer."""

    def __init__(self, tool="browser_open", arguments='{"url": "https://example.amocrm.ru/leads"}') -> None:
        self.requests = []
        self.tool = tool
        self.arguments = arguments

    def create(self, **kwargs):
        self.requests.append(kwargs)
        n = len(self.requests)
        tool, arguments = self.tool, self.arguments

        class Usage:
            prompt_tokens = 10
            completion_tokens = 4

        class Function:
            name = tool

        Function.arguments = arguments

        class Call:
            id = "call_b1"
            function = Function()

        class Message:
            content = '{"task_type":"crm","complexity":1}' if n == 1 else ("" if n == 2 else "Сделок 3")
            tool_calls = [Call()] if n == 2 else None

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


def _sse_events(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_stream_runs_client_tool_and_records_trajectory(client, session, monkeypatch) -> None:
    user = User(
        email="loop@example.com",
        consent_pdn=True,
        consent_training=True,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    task = start_task(session, user, "Сколько сделок в amoCRM?", {})
    session.commit()
    script = _BrowserScript()
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    client.app.dependency_overrides[get_heli_client] = lambda: HeliClient(sdk)
    monkeypatch.setattr(api, "get_run_session_factory", lambda: sessionmaker(bind=session.get_bind()))
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}

    def answer_client():
        for _ in range(200):
            agent_run = runs.registry.get(task.id, user.id)
            if agent_run and any(e["type"] == "client_action" for e in agent_run.events):
                break
            time.sleep(0.02)
        r = client.post(
            f"/v1/tasks/{task.id}/tool-results",
            json={"call_id": "call_b1", "content": "Адрес: https://example.amocrm.ru/leads\nСделки: 3"},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    helper = threading.Thread(target=answer_client)
    helper.start()
    response = client.post(
        f"/v1/tasks/{task.id}/run",
        json={"client_tools": ["browser_open", "browser_read"]},
        headers={**headers, "Accept": "text/event-stream"},
    )
    helper.join(5)
    assert response.status_code == 200
    events = _sse_events(response.text)
    types = [e["type"] for e in events]
    assert types[0] == "started" and types[-1] == "done"
    action = next(e for e in events if e["type"] == "client_action")
    assert action["tool"] == "browser_open" and action["args"]["url"].startswith("https://example.amocrm.ru")
    assert events[-1]["reply"] == "Сделок 3" and events[-1]["stopped"] is None
    names = {t["function"]["name"] for t in script.requests[1]["tools"]}
    assert {"web_search", "open_url", "browser_open", "browser_read"} <= names
    assert "browser_click" not in names
    assert "браузер на компьютере пользователя" in script.requests[1]["messages"][0]["content"]

    steps = list(session.scalars(select(TaskStep).where(TaskStep.task_id == task.id).order_by(TaskStep.position)))
    assert [s.thought for s in steps] == ["классификация", "инструменты", "инструмент", "ответ"]
    assert steps[2].tool_name == "browser_open" and steps[2].observation_type == "dom"
    assert "Сделки: 3" in steps[2].result_text

    replay = client.get(f"/v1/tasks/{task.id}/events?after=0", headers=headers)
    assert [e["type"] for e in _sse_events(replay.text)] == types
    other = client.post(f"/v1/tasks/{task.id}/control", json={"action": "stop"}, headers=headers)
    assert other.json()["finished"] is True


def test_eval_entry_point_uses_the_same_loop() -> None:
    from app.agent import evals

    script = _BrowserScript()
    script.requests.append({})  # skip the classify reply: evals do not classify
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    calls = []
    tools = [{"type": "function", "function": {"name": "browser_open", "parameters": {}}}]
    result = evals.run(
        "Сколько сделок?",
        tools,
        lambda name, args: calls.append((name, args)) or {"content": "Сделки: 3"},
        {"max_steps": 5, "max_rub": 100},
        client=HeliClient(sdk),
    )
    assert calls == [("browser_open", {"url": "https://example.amocrm.ru/leads"})]
    assert result["final_answer"] == "Сделок 3" and result["stopped"] is None and result["steps"] == 2


def _run_with_client(client, session, monkeypatch, request_text, script, client_tools, answer):
    user = User(
        email=f"u{time.monotonic_ns()}@example.com",
        consent_pdn=True,
        consent_training=True,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    task = start_task(session, user, request_text, {})
    session.commit()
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    client.app.dependency_overrides[get_heli_client] = lambda: HeliClient(sdk)
    monkeypatch.setattr(api, "get_run_session_factory", lambda: sessionmaker(bind=session.get_bind()))
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}

    def answer_client():
        for _ in range(200):
            agent_run = runs.registry.get(task.id, user.id)
            action = agent_run and next((e for e in agent_run.events if e["type"] == "client_action"), None)
            if action:
                client.post(f"/v1/tasks/{task.id}/tool-results", json={"call_id": action["call_id"], **answer}, headers=headers)
                return
            time.sleep(0.02)

    helper = threading.Thread(target=answer_client)
    helper.start()
    response = client.post(
        f"/v1/tasks/{task.id}/run",
        json={"client_tools": client_tools},
        headers={**headers, "Accept": "text/event-stream"},
    )
    helper.join(5)
    return _sse_events(response.text)


def test_search_goes_through_the_users_browser_without_api_key(client, session, monkeypatch) -> None:
    from app.llm import tools as server_tools

    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("HELI_SEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(server_tools, "web_search", lambda q: pytest.fail("server search must not run"))
    script = _BrowserScript("web_search", '{"query": "amoCRM цены"}')
    events = _run_with_client(
        client, session, monkeypatch, "Сколько стоит amoCRM?", script,
        ["browser_open", "browser_search"], {"content": "1. amoCRM — тарифы\nhttps://www.amocrm.ru/prices"},
    )
    action = next(e for e in events if e["type"] == "client_action")
    assert action["tool"] == "browser_search" and action["args"] == {"query": "amoCRM цены"}
    assert "amocrm.ru/prices" in script.requests[2]["messages"][-1]["content"]
    assert events[-1]["type"] == "done"


def test_browser_search_falls_back_to_server_search_on_captcha(client, session, monkeypatch) -> None:
    from app.llm import tools as server_tools
    from app.web import SearchHit

    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("HELI_SEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(server_tools, "web_search", lambda q: [SearchHit("Вики", "https://ru.wikipedia.org/x", "фрагмент")])
    script = _BrowserScript("web_search", '{"query": "amoCRM"}')
    events = _run_with_client(
        client, session, monkeypatch, "Что такое amoCRM?", script,
        ["browser_search"], {"content": "", "error": "captcha"},
    )
    assert "ru.wikipedia.org" in script.requests[2]["messages"][-1]["content"]
    assert events[-1]["type"] == "done"


def test_cloud_executor_runs_browser_tools_on_the_server(client, session, monkeypatch) -> None:
    from app.agent import executors

    ran = []

    def cloud(db, user, task_id, name, args):
        ran.append((name, args))
        return ClientResult("Адрес: https://example.amocrm.ru/leads\nСделки: 3")

    executors.register_executor({"browser_open", "browser_read"}, cloud)
    try:
        script = _BrowserScript()
        events = _run_with_client(
            client, session, monkeypatch, "Сколько сделок?", script, [], {"content": "не должен вызываться"}
        )
    finally:
        executors.unregister_executor({"browser_open", "browser_read"})
    assert ran == [("browser_open", {"url": "https://example.amocrm.ru/leads"})]
    assert not any(e["type"] == "client_action" for e in events)
    assert events[-1]["reply"] == "Сделок 3"


# --- confirmation gate -----------------------------------------------------------------------

GATED_TOOLS = [
    {"type": "function", "function": {"name": "telegram_send", "parameters": {}}},
    {"type": "function", "function": {"name": "ask_confirmation", "parameters": {}}},
    {"type": "function", "function": {"name": "amocrm_list_deals", "parameters": {}}},
]


def test_risky_tool_never_runs_without_approval() -> None:
    complete = _scripted(_completion(calls=[("telegram_send", {"chat": "client", "text": "Счёт готов"})]))
    called = []

    def call_tool(cid, name, args):
        called.append((name, args))
        return ToolResult("rejected")

    out = run_agent([], tools=GATED_TOOLS, call_tool=call_tool, complete=complete, budget=Budget())
    assert [name for name, _ in called] == ["ask_confirmation"]
    assert called[0][1]["action"] == "telegram_send" and "Счёт готов" in called[0][1]["details"]
    assert out.stopped == "confirm"


def test_loop_asks_itself_and_runs_after_approval() -> None:
    complete = _scripted(_completion(calls=[("telegram_send", {"text": "x"})]), _completion("Отправил"))
    called = []

    def call_tool(cid, name, args):
        called.append(name)
        return ToolResult("approved" if name == "ask_confirmation" else "ok")

    out = run_agent([], tools=GATED_TOOLS, call_tool=call_tool, complete=complete, budget=Budget())
    assert called == ["ask_confirmation", "telegram_send"] and out.stopped is None


def test_one_approval_covers_one_action() -> None:
    complete = _scripted(
        _completion(calls=[("ask_confirmation", {"action": "telegram_send"})]),
        _completion(calls=[("telegram_send", {"text": "1"}), ("telegram_send", {"text": "2"})]),
        _completion("ok"),
    )
    called = []

    def call_tool(cid, name, args):
        called.append(name)
        if name == "ask_confirmation":
            return ToolResult("approved" if len(called) == 1 else "rejected")
        return ToolResult("ok")

    out = run_agent([], tools=GATED_TOOLS, call_tool=call_tool, complete=complete, budget=Budget())
    assert called == ["ask_confirmation", "telegram_send", "ask_confirmation"]
    assert out.stopped == "confirm"


def test_reading_tools_are_not_gated() -> None:
    complete = _scripted(_completion(calls=[("amocrm_list_deals", {})]), _completion("3 сделки"))
    called = []
    run_agent(
        [], tools=GATED_TOOLS, call_tool=lambda c, n, a: called.append(n) or ToolResult("[]"),
        complete=complete, budget=Budget(),
    )
    assert called == ["amocrm_list_deals"]


def test_browser_click_on_send_button_is_gated() -> None:
    page = "Адрес: https://mail.example\n\nЭлементы:\n[1] поле «Кому»\n[2] кнопка «Отправить»"
    complete = _scripted(
        _completion(calls=[("browser_read", {})]),
        _completion(calls=[("browser_click", {"ref": 1}), ("browser_click", {"ref": 2})]),
    )
    called = []

    def call_tool(cid, name, args):
        called.append((name, args))
        if name == "browser_read":
            return ToolResult(page)
        return ToolResult("rejected" if name == "ask_confirmation" else "ok")

    tools = GATED_TOOLS + [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("browser_read", "browser_click")]
    out = run_agent([], tools=tools, call_tool=call_tool, complete=complete, budget=Budget())
    assert [n for n, _ in called] == ["browser_read", "browser_click", "ask_confirmation"]
    assert "Отправить" in called[-1][1]["details"] and out.stopped == "confirm"


def test_tool_call_budget_forces_an_answer() -> None:
    complete = _scripted(
        _completion(calls=[("amocrm_list_deals", {}), ("amocrm_list_deals", {})]),
        _completion(calls=[("amocrm_list_deals", {})]),
        _completion("Итог: 3 сделки"),
    )
    called = []
    out = run_agent(
        [], tools=GATED_TOOLS, call_tool=lambda c, n, a: called.append(n) or ToolResult("[]"),
        complete=complete, budget=Budget(max_steps=10, max_tool_calls=3),
    )
    assert len(called) == 3
    assert complete.seen[2][1] is None and "Лимит действий" in complete.seen[2][0][-1]["content"]
    assert out.final_answer == "Итог: 3 сделки"


def test_eval_adapter_reads_mock_approvals() -> None:
    from evals.world import World
    from app.agent import evals

    world = World.build(None, "approve")
    script = _BrowserScript("telegram_send", '{"chat_id": "c1", "text": "Привет"}')
    script.requests.append({})
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    evals.run("Напиши клиенту", GATED_TOOLS, world.call, {"max_steps": 12}, client=HeliClient(sdk))
    assert [c.tool for c in world.calls][:2] == ["ask_confirmation", "telegram_send"]
    assert world.violations == []
