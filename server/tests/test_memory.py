import json
from datetime import datetime, timezone

from app.auth import create_invite
from app.llm.client import HeliClient
from app.llm.run import run_turn
from app.memory import delete_memory, relevant, remember
from app.models import User
from app.trajectories import start_task


def _user(session) -> User:
    user = User(
        email="memory@example.com",
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def test_word_search_and_delete(session) -> None:
    user = _user(session)
    contract = remember(session, user, "Договор поставки действует до декабря")
    remember(session, user, "Кот на складе не живёт")
    found = relevant(session, user, "договор")
    assert found[0] == contract.text
    delete_memory(session, user, contract.id)
    assert relevant(session, user, "договор") == []


def test_vectors_rank_without_shared_words(session) -> None:
    user = _user(session)

    def embedder(text: str) -> list[float]:
        return {
            "alpha": [1.0, 0.0],
            "beta": [0.0, 1.0],
            "question": [0.9, 0.1],
        }[text]

    remember(session, user, "alpha", embedder=embedder)
    remember(session, user, "beta", embedder=embedder)
    assert relevant(session, user, "question", embedder=embedder)[0] == "alpha"


class _Scripted:
    def __init__(self) -> None:
        self.messages: list = []
        self.texts = ['{"task_type":"status","complexity":1}', "Готово"]

    def create(self, **kwargs):
        self.messages.append(kwargs["messages"])
        text = self.texts.pop(0)

        class Message:
            content = text

        class Choice:
            message = Message()

        class Usage:
            prompt_tokens = 8
            completion_tokens = 2

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


def test_turn_includes_memory(session) -> None:
    user = _user(session)
    remember(session, user, "Оплата по пятницам")
    task = start_task(session, user, "Оплата", {})
    script = _Scripted()
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    run_turn(session, user, task, HeliClient(sdk))
    prompt = script.messages[0][1]["content"]
    assert "Оплата по пятницам" in prompt
    assert "Память" in prompt


def test_memory_http_hides_vectors(client, session) -> None:
    invite = create_invite(session)
    session.commit()
    issued = client.post(
        "/v1/auth/request-code",
        json={"email": "facts@example.com", "invite_code": invite},
    )
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": "facts@example.com", "code": code}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/v1/memory", headers=headers, json={"text": "Склад на Лесной"})
    assert created.status_code == 201
    assert set(created.json()) == {"id", "text"}
    raw = json.dumps(client.get("/v1/memory", headers=headers).json())
    assert "embedding" not in raw
    assert "openai" not in raw.lower()
    removed = client.delete(f"/v1/memory/{created.json()['id']}", headers=headers)
    assert removed.status_code == 200
    assert client.get("/v1/memory", headers=headers).json()["facts"] == []
