import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth import create_invite, issue_login_code, issue_token, verify_login_code
from app.models import Trajectory, User
from app.trajectories import add_step, finish_task, rate_task, set_consents, start_task


def _user(session, *, training: bool, admin: bool = False) -> User:
    user = User(
        email="owner@example.com",
        consent_pdn=True,
        consent_training=training,
        is_admin=admin,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def _record(session, user: User, *, outcome: str = "success"):
    task = start_task(
        session,
        user,
        "Напиши письмо на ivan@example.com, телефон +7 999 123-45-67, карта 4242424242424242",
        {"memory_ids": ["m1"], "note": "ИНН 7707083893 и паспорт 4510 123456"},
    )
    add_step(
        session,
        user,
        task.id,
        thought="Открою форму",
        plan="Сначала DOM",
        model_alias="heli-fast",
        reply="Готово, deepseek не должен остаться в выгрузке",
        tool_name="type",
        tool_args={"password": "hunter2", "text": "ivan@example.com"},
        result_text="введено",
        observation_type="dom",
        observation_text="<input value='ivan@example.com'>",
        input_tokens=120,
        output_tokens=30,
        cost_rub=Decimal("0.02"),
        latency_ms=800,
        attempt=1,
    )
    add_step(
        session,
        user,
        task.id,
        thought="Повтор после ошибки",
        model_alias="heli-fast",
        error="timeout",
        attempt=2,
        observation_type="screenshot",
        screenshot_ref="shots/1",
        screenshot=b"png",
    )
    finish_task(session, user, task.id, outcome, "Письмо не отправлено: ivan@example.com")
    return task


def test_without_training_consent_nothing_is_stored_for_export(session) -> None:
    user = _user(session, training=False)
    task = _record(session, user)
    assert session.scalar(select(Trajectory).where(Trajectory.task_id == task.id)) is None


def test_consented_document_masks_pii_and_keeps_alias(session, monkeypatch) -> None:
    monkeypatch.setenv("HELI_API_KEY", "unit-test-credential")
    user = _user(session, training=True)
    task = start_task(session, user, "ключ unit-test-credential и почта a@b.co", {"memory_ids": ["m1"]})
    add_step(
        session,
        user,
        task.id,
        model_alias="heli-smart",
        reply="unit-test-credential",
        tool_name="fill",
        tool_args={"password": "secret-pass", "login": "a@b.co"},
        thought="план",
        plan="шаг",
        result_text="ok",
        observation_type="dom",
        observation_text="телефон +7 999 123-45-67",
        input_tokens=10,
        output_tokens=4,
        cost_rub="0.010000",
        latency_ms=15,
        error=None,
        attempt=1,
    )
    finish_task(session, user, task.id, "success", "ответ a@b.co")
    rate_task(session, user, task.id, rating=1, acceptance="accepted")
    row = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
    assert row is not None
    blob = json.dumps(row.payload, ensure_ascii=False)
    if "unit-test-credential" in blob or "secret-pass" in blob or "a@b.co" in blob:
        raise AssertionError("sensitive value leaked into the trajectory")
    if str(user.id) in blob or user.email in blob:
        raise AssertionError("user identity leaked into the trajectory")
    assert row.payload["user_hash"] != str(user.id)
    assert row.payload["steps"][0]["model"] == "heli-smart"
    assert row.payload["steps"][0]["tool_call"]["args"]["password"] == "<CREDENTIAL>"
    assert row.payload["steps"][0]["input_tokens"] == 10
    assert row.payload["steps"][0]["cost_rub"] == "0.010000"
    assert "[phone]" in row.payload["steps"][0]["observation"]["text"]


def test_full_step_fields_and_retry(session) -> None:
    user = _user(session, training=True)
    task = _record(session, user)
    payload = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id)).payload
    assert payload["context"]["memory_ids"] == ["m1"]
    assert payload["result"] == "success"
    assert payload["steps"][0]["thought"] == "Открою форму"
    assert payload["steps"][0]["plan"] == "Сначала DOM"
    assert payload["steps"][1]["attempt"] == 2
    assert payload["steps"][1]["error"] == "timeout"
    assert payload["steps"][1]["observation"]["type"] == "screenshot"
    assert payload["steps"][1]["observation"]["ref"] == "shots/1"
    blob = json.dumps(payload, ensure_ascii=False)
    assert "png" not in blob
    assert "[email]" in payload["task"]
    assert "[card]" in payload["task"]
    assert "[inn]" in json.dumps(payload["context"], ensure_ascii=False)
    assert "[passport]" in json.dumps(payload["context"], ensure_ascii=False)
    assert "[model]" in blob
    if "deepseek" in blob:
        raise AssertionError("upstream name leaked into the trajectory")


def test_success_filter_sft_and_dpo(session) -> None:
    from app.trajectories import list_documents, to_dpo, to_sft

    user = _user(session, training=True)
    good = _record(session, user, outcome="success")
    rate_task(
        session,
        user,
        good.id,
        rating=-1,
        acceptance="edited",
        user_edit="Короткий ответ без почты",
    )
    add_step(
        session,
        user,
        good.id,
        thought="клик",
        tool_name="click",
        tool_args={"x": 1},
        tool_args_edit={"x": 2},
        confirmation="edited",
        model_alias="heli-fast",
    )
    # Rating after an extra step rebuilds the document.
    rate_task(session, user, good.id, rating=-1, acceptance="edited", user_edit="Короткий ответ без почты")
    bad = start_task(session, user, "вторая", {})
    finish_task(session, user, bad.id, "fail", "не вышло")

    successes = list_documents(session, success_only=True)
    assert {item["id"] for item in successes} == {str(good.id)}
    sft = to_sft(successes[0])
    assert sft["messages"][0]["role"] == "user"
    assert sft["messages"][1]["role"] == "assistant"
    assert sft["outcome"] == "success"
    pairs = to_dpo(successes[0])
    kinds = {pair["kind"] for pair in pairs}
    assert "answer" in kinds
    assert "action" in kinds


def test_withdraw_consent_deletes_documents(session) -> None:
    user = _user(session, training=True)
    task = _record(session, user)
    assert session.scalar(select(Trajectory).where(Trajectory.task_id == task.id)) is not None
    set_consents(session, user, pdn=True, training=False)
    assert session.scalar(select(Trajectory).where(Trajectory.task_id == task.id)) is None


def test_unknown_alias_is_rejected_without_echo(session) -> None:
    from app.trajectories import TrajectoryError

    user = _user(session, training=True)
    task = start_task(session, user, "задача", {})
    with pytest.raises(TrajectoryError) as caught:
        add_step(session, user, task.id, model_alias="upstream/secret-model")
    assert caught.value.code == "unknown_alias"
    assert "upstream" not in caught.value.code


def test_login_and_export_roundtrip(client, session) -> None:
    invite = create_invite(session, max_uses=2)
    session.commit()
    refused = client.post("/v1/auth/request-code", json={"email": "a@example.com", "invite_code": "nope"})
    assert refused.status_code == 400
    issued = client.post(
        "/v1/auth/request-code",
        json={"email": "a@example.com", "invite_code": invite},
    )
    assert issued.status_code == 202
    assert "code" not in issued.json()
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": "a@example.com", "code": code}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.put("/v1/me/consents", headers=headers, json={"consent_pdn": True, "consent_training": True})
    task_id = client.post("/v1/tasks", headers=headers, json={"request": "привет a@example.com", "context": {}}).json()["id"]
    step = client.post(
        f"/v1/tasks/{task_id}/steps",
        headers=headers,
        json={
            "model_alias": "heli-fast",
            "thought": "думаю",
            "reply": "ответ",
            "tool_name": "click",
            "tool_args": {"x": 3, "password": "nope"},
            "input_tokens": 5,
            "output_tokens": 2,
            "cost_rub": "0.001",
            "attempt": 1,
            "observation_type": "dom",
            "observation_text": "дерево",
        },
    )
    assert step.status_code == 201
    assert step.json()["model"] == "heli-fast"
    bad_alias = client.post(
        f"/v1/tasks/{task_id}/steps",
        headers=headers,
        json={"model_alias": "upstream/secret-model"},
    )
    assert bad_alias.status_code == 400
    assert "upstream" not in bad_alias.text
    client.post(
        f"/v1/tasks/{task_id}/finish",
        headers=headers,
        json={"outcome": "success", "final_answer": "готово"},
    )
    client.post(
        f"/v1/tasks/{task_id}/feedback",
        headers=headers,
        json={"rating": 1, "acceptance": "accepted"},
    )
    forbidden = client.get("/v1/admin/trajectories.jsonl?kind=sft")
    assert forbidden.status_code == 401
    denied = client.get("/v1/admin/trajectories.jsonl?kind=sft", headers=headers)
    assert denied.status_code == 403

    user = session.scalar(select(User).where(User.email == "a@example.com"))
    user.is_admin = True
    session.commit()
    exported = client.get("/v1/admin/trajectories.jsonl?kind=sft", headers=headers)
    assert exported.status_code == 200
    line = json.loads(exported.text.strip())
    assert line["outcome"] == "success"
    assert "a@example.com" not in exported.text
    raw = client.get("/v1/admin/trajectories.jsonl?kind=raw&success_only=false", headers=headers)
    assert "heli-fast" in raw.text
    failed = client.get("/v1/admin/trajectories.jsonl?kind=sft&success_only=true", headers=headers)
    assert failed.status_code == 200


def test_log_filter_hides_credential(caplog, monkeypatch) -> None:
    from app.masking import install_redact_filter

    monkeypatch.setenv("HELI_API_KEY", "unit-test-credential")
    install_redact_filter()
    caplog.set_level(logging.INFO)
    logging.getLogger("app.probe").info("value %s", os.environ["HELI_API_KEY"])
    if "unit-test-credential" in caplog.text:
        raise AssertionError("credential appeared in logs")


def test_migration_upgrades_sqlite(tmp_path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/heli.db"
    monkeypatch.setenv("DATABASE_URL", url)
    root = tmp_path.parents[0]
    # server directory is the parent of tests
    server_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    cfg = Config(str(server_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(server_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def test_issue_code_helper_does_not_need_http(session) -> None:
    invite = create_invite(session)
    code = issue_login_code(session, "b@example.com", invite)
    user = verify_login_code(session, "b@example.com", code)
    token = issue_token(user.id)
    assert token
    assert "b@example.com" not in token
