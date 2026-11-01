import base64
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.api import get_heli_client
from app.auth import create_invite
from app.computer import (
    COMPUTER_TOOL,
    ComputerError,
    HistoryItem,
    decode_screenshot,
    next_step,
    parse_action,
    risk_of,
)
from app.llm.client import HeliClient
from app.models import TaskStep, User
from app.quota import estimate_input_tokens
from app.trajectories import start_task

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SHOT = base64.b64encode(PNG).decode()


class _Vision:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.texts.pop(0)

        class Message:
            content = text
            tool_calls = None

        class Choice:
            message = Message()

        class Usage:
            prompt_tokens = 1700
            completion_tokens = 60

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


def _client(script: _Vision) -> HeliClient:
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    return HeliClient(sdk)


@pytest.fixture(autouse=True)
def _pc_hands_on(monkeypatch):
    monkeypatch.setenv("HELI_PC_HANDS", "true")


def _user(session) -> User:
    user = User(
        email="hands@example.com",
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def _reply(**fields) -> str:
    return "Вот шаг:\n" + json.dumps(fields, ensure_ascii=False)


def test_tool_schema_is_openai_function() -> None:
    assert COMPUTER_TOOL["function"]["name"] == "use_computer"
    assert COMPUTER_TOOL["function"]["parameters"]["required"] == ["goal"]


def test_parse_click_keeps_grid_point_and_target() -> None:
    action, thought, flagged = parse_action(
        _reply(thought="вижу кнопку", action="click", x=512.4, y=300, target="кнопка «Записать»")
    )
    assert action == {"type": "click", "x": 512, "y": 300, "target": "кнопка «Записать»"}
    assert thought == "вижу кнопку"
    assert flagged is False
    assert risk_of(action, flagged) == "write"


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "click", "x": 1200, "y": 5},
        {"action": "click", "x": 5},
        {"action": "type", "text": ""},
        {"action": "key", "keys": "hyper+x"},
        {"action": "key", "keys": "ctrl+ж"},
        {"action": "scroll", "x": 1, "y": 1, "amount": 0},
        {"action": "wait", "seconds": 99},
        {"action": "run_command"},
    ],
)
def test_parse_rejects_bad_actions(fields) -> None:
    with pytest.raises(ComputerError):
        parse_action(_reply(**fields))


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ComputerError):
        parse_action("Нажми кнопку")


def test_outward_actions_are_dangerous_even_if_model_forgets() -> None:
    action, _, flagged = parse_action(_reply(action="click", x=1, y=1, target="кнопка «Провести и закрыть»"))
    assert flagged and risk_of(action, flagged) == "danger"
    action, _, flagged = parse_action(_reply(action="click", x=1, y=1, target="Отправить письмо"))
    assert risk_of(action, flagged) == "danger"
    action, _, flagged = parse_action(_reply(action="click", x=1, y=1, target="поле «Сумма»", danger=True))
    assert risk_of(action, flagged) == "danger"


def test_key_risk_matches_desktop_rules() -> None:
    for keys in ["alt+f4", "win+r", "shift+delete", "ctrl+alt+delete", "ctrl+w", "ctrl+shift+esc"]:
        action, _, flagged = parse_action(_reply(action="key", keys=keys))
        assert risk_of(action, flagged) == "danger", keys
    action, _, flagged = parse_action(_reply(action="key", keys="Ctrl+S"))
    assert action["keys"] == "ctrl+s"
    assert risk_of(action, flagged) == "write"
    action, _, flagged = parse_action(_reply(action="scroll", x=500, y=500, amount=-3))
    assert risk_of(action, flagged) == "read"


def test_done_and_fail_carry_summary() -> None:
    action, _, _ = parse_action(_reply(action="done", summary="Счёт проведён"))
    assert action == {"type": "done", "summary": "Счёт проведён"}
    action, _, _ = parse_action(_reply(action="fail"))
    assert action["summary"]


def test_screenshot_must_be_png_or_jpeg() -> None:
    assert decode_screenshot(SHOT)[1] == "image/png"
    assert decode_screenshot("data:image/png;base64," + SHOT)[1] == "image/png"
    with pytest.raises(ComputerError):
        decode_screenshot(base64.b64encode(b"GIF89a....").decode())
    with pytest.raises(ComputerError):
        decode_screenshot("not base64!")


def test_image_counts_as_fixed_tokens_not_base64_length() -> None:
    big = "A" * 400_000
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "x" * 40}, {"type": "image_url", "image_url": {"url": big}}]}
    ]
    assert estimate_input_tokens(messages) == 10 + 1600


def test_next_step_sends_image_to_vision_and_logs_step(session) -> None:
    user = _user(session)
    task = start_task(session, user, "Проведи счёт 123 в 1С", {})
    script = _Vision([_reply(thought="поле поиска", action="type", text="123", x=100, y=40, target="поиск")])
    step = next_step(
        session,
        user,
        task.id,
        _client(script),
        goal="Открыть счёт 123",
        screenshot_base64=SHOT,
        history=[HistoryItem(action="click", target="Документы", ok=True)],
        step=2,
    )
    assert step.action == {"type": "type", "text": "123", "x": 100, "y": 40, "target": "поиск"}
    assert step.alias == "heli-vision"
    assert step.risk == "write"
    sent = script.calls[0]["messages"][1]["content"]
    assert sent[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "Документы" in sent[0]["text"]
    row = session.scalar(select(TaskStep).where(TaskStep.task_id == task.id))
    assert row.tool_name == "computer.type"
    assert row.observation_type == "screenshot"
    assert row.screenshot == PNG
    assert row.model_alias == "heli-vision"


def test_repeated_failures_escalate_to_smart_model(session) -> None:
    user = _user(session)
    task = start_task(session, user, "задача", {})
    script = _Vision([_reply(action="fail", summary="Нужен пароль")])
    history = [HistoryItem(action="click", ok=False), HistoryItem(action="click", ok=False)]
    step = next_step(
        session, user, task.id, _client(script), goal="войти", screenshot_base64=SHOT, history=history, step=3
    )
    assert step.alias == "heli-smart"
    assert step.finished is True


def test_bad_model_reply_becomes_short_wait_with_error(session) -> None:
    user = _user(session)
    task = start_task(session, user, "задача", {})
    step = next_step(
        session, user, task.id, _client(_Vision(["не знаю"])), goal="цель", screenshot_base64=SHOT, history=[], step=1
    )
    assert step.action == {"type": "wait", "seconds": 1}
    assert step.error == "bad_model_reply"


def test_endpoint_returns_action(client, session) -> None:
    invite = create_invite(session)
    session.commit()
    client.post("/v1/auth/request-code", json={"email": "pc@example.com", "invite_code": invite})
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": "pc@example.com", "code": code}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.put("/v1/me/consents", headers=headers, json={"consent_pdn": True, "consent_training": False})
    created = client.post("/v1/tasks", headers=headers, json={"request": "1С", "context": {}})
    task_id = created.json()["id"]
    script = _Vision([_reply(action="click", x=10, y=20, target="Оплатить")])
    client.app.dependency_overrides[get_heli_client] = lambda: _client(script)
    response = client.post(
        f"/v1/tasks/{task_id}/computer/step",
        headers=headers,
        json={"goal": "Открыть 1С", "screenshot_base64": SHOT, "history": [], "step": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["type"] == "click"
    assert body["risk"] == "danger"
    assert body["finished"] is False
    too_far = client.post(
        f"/v1/tasks/{task_id}/computer/step",
        headers=headers,
        json={"goal": "Открыть 1С", "screenshot_base64": SHOT, "step": 31},
    )
    assert too_far.status_code == 409


def test_pc_hands_are_off_by_default(session, monkeypatch) -> None:
    monkeypatch.delenv("HELI_PC_HANDS")
    user = _user(session)
    task = start_task(session, user, "задача", {})
    script = _Vision([_reply(action="done")])
    with pytest.raises(ComputerError) as exc:
        next_step(session, user, task.id, _client(script), goal="цель", screenshot_base64=SHOT, history=[], step=1)
    assert exc.value.code == "pc_hands_disabled"
    assert script.calls == []
