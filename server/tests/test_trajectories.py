import json
import logging
import os
import uuid
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
    status = client.get("/v1/admin/trajectories/status", headers=headers).json()
    assert status["trajectories"] >= 1 and status["tasks_with_outcome"] >= 1 and "hint" in status


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


def test_auto_finish_closes_unrated_task_and_rating_overrides(session) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.models import Trajectory, User
    from app.trajectories import auto_finish, finish_task, start_task

    user = User(
        email="auto@example.com",
        consent_pdn=True,
        consent_training=True,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    task = start_task(session, user, "Найди аренду видеокарты", {})
    auto_finish(session, task.id, "success", "Терафeрма от 12 ₽/ч")
    assert task.outcome == "success"
    doc = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
    assert doc is not None and doc.outcome == "success"
    # The run never overrides an outcome that is already set.
    auto_finish(session, task.id, "fail", None)
    assert task.outcome == "success"
    # The user's «Не сработало» does, and keeps the run's answer when it sends none.
    finish_task(session, user, task.id, "fail", "")
    assert task.outcome == "fail" and task.final_answer == "Терафeрма от 12 ₽/ч"
    assert session.scalar(select(Trajectory).where(Trajectory.task_id == task.id)).outcome == "fail"


# --- self-learning signals (server/LEARNING.md) ------------------------------------------


def _api_user(client, session, email: str, *, training: bool = True, admin: bool = False):
    user = User(
        email=email,
        consent_pdn=True,
        consent_training=training,
        is_admin=admin,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.commit()
    return user, {"Authorization": f"Bearer {issue_token(user.id)}"}


def _doc(session, task_id):
    session.expire_all()
    row = session.scalar(select(Trajectory).where(Trajectory.task_id == task_id))
    return row.payload if row is not None else None


def _lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_feedback_reason_and_fixed_by_hand_reach_payload_and_dpo(client, session) -> None:
    from app.trajectories import auto_finish, to_dpo

    user, headers = _api_user(client, session, "fix@example.com")
    task = start_task(session, user, "Посчитай остатки на складе, мой телефон +7 999 123-45-67", {})
    add_step(session, user, task.id, model_alias="heli-fast", reply="Остаток 10 шт.", cost_rub="0.5")
    add_step(session, user, task.id, model_alias="heli-fast", reply="ещё", cost_rub="0.25")
    auto_finish(session, task.id, "success", "Остаток 10 шт.")
    session.commit()

    bad = client.post(f"/v1/tasks/{task.id}/feedback", headers=headers, json={"rating": -1, "reason_code": "nope"})
    assert bad.status_code == 400 and bad.json()["detail"] == "bad_reason_code"
    bad_mail = client.post(f"/v1/tasks/{task.id}/feedback", headers=headers, json={"mail_disposition": "lost"})
    assert bad_mail.status_code == 400

    # An old client: only rating/acceptance/comment still works.
    old = client.post(f"/v1/tasks/{task.id}/feedback", headers=headers, json={"rating": -1, "acceptance": "rejected"})
    assert old.status_code == 200
    reason = client.post(f"/v1/tasks/{task.id}/feedback", headers=headers, json={"rating": -1, "reason_code": "wrong_fact"})
    assert reason.json()["reason_code"] == "wrong_fact"
    fixed = client.post(
        f"/v1/tasks/{task.id}/feedback",
        headers=headers,
        json={"user_fixed": True, "user_edit": "Остаток 12 шт., звонить +7 999 123-45-67"},
    )
    assert fixed.status_code == 200
    assert fixed.json()["acceptance"] == "edited" and fixed.json()["rating"] == -1

    payload = _doc(session, task.id)
    assert payload["feedback"]["reason_code"] == "wrong_fact"
    assert payload["feedback"]["user_fixed"] is True
    assert payload["cost_rub_total"] == "0.750000"
    assert payload["stopped"] is None and payload["duration_ms"] is not None
    assert "revision" in payload and payload["internal"] is False
    blob = json.dumps(payload, ensure_ascii=False)
    assert "999 123" not in blob and str(user.id) not in blob and user.email not in blob
    pairs = to_dpo(payload)
    assert [p["kind"] for p in pairs] == ["answer"]
    assert "12 шт." in pairs[0]["chosen"] and pairs[0]["rejected"] == "Остаток 10 шт."


def test_sft_skips_bare_auto_success_and_takes_confirmed_ones(client, session) -> None:
    from app.trajectories import auto_finish

    admin, headers = _api_user(client, session, "admin@example.com", training=False, admin=True)
    user, user_headers = _api_user(client, session, "tester@example.com")
    bare = start_task(session, user, "Найди аренду видеокарты", {})
    auto_finish(session, bare.id, "success", "Терафeрма от 12 ₽/ч")
    liked = start_task(session, user, "Составь план поста про скидки", {})
    auto_finish(session, liked.id, "success", "План: …")
    rate_task(session, user, liked.id, rating=1, acceptance="accepted")
    checked = start_task(session, user, "Создай сделку в Битрикс24 и проверь", {})
    add_step(session, user, checked.id, tool_name="bitrix24_write", tool_args={"method": "crm.deal.add"}, result_text="id 7")
    add_step(session, user, checked.id, tool_name="bitrix24_read", tool_args={"method": "crm.deal.get"}, result_text="сделка 7")
    auto_finish(session, checked.id, "success", "Сделка создана и проверена")
    session.commit()

    assert [s["kind"] for s in _doc(session, checked.id)["signals"]] == ["verify_ok"]
    sft = _lines(client.get("/v1/admin/trajectories.jsonl?kind=sft", headers=headers).text)
    assert {line["id"] for line in sft} == {str(liked.id), str(checked.id)}
    raw = _lines(client.get("/v1/admin/trajectories.jsonl?kind=raw", headers=headers).text)
    assert str(bare.id) in {line["id"] for line in raw}
    thumbs = _lines(client.get("/v1/admin/trajectories.jsonl?kind=sft&thumbs_up_only=true", headers=headers).text)
    assert {line["id"] for line in thumbs} == {str(liked.id)}


def test_open_access_and_training_accounts_stay_out_of_sft_and_dpo(client, session, monkeypatch) -> None:
    from app.trajectories import auto_finish

    monkeypatch.setenv("HELI_TRAINING_EMAILS", "trainer@heli.local")
    admin, headers = _api_user(client, session, "boss@example.com", training=False, admin=True)
    for email in ("owner@heli.local", "trainer@heli.local"):
        shared = User(email=email, consent_pdn=True, consent_training=True, created_at=datetime.now(timezone.utc))
        session.add(shared)
        session.flush()
        task = start_task(session, shared, "Задача общего аккаунта", {})
        auto_finish(session, task.id, "success", "Готово")
        rate_task(session, shared, task.id, rating=1, acceptance="accepted")
        other = start_task(session, shared, "Другая задача про рекламу", {})
        auto_finish(session, other.id, "success", "Ответ бота")
        rate_task(session, shared, other.id, user_fixed=True, user_edit="Правильный ответ")
    session.commit()

    assert client.get("/v1/admin/trajectories.jsonl?kind=sft", headers=headers).text == ""
    assert client.get("/v1/admin/trajectories.jsonl?kind=dpo", headers=headers).text == ""
    raw = _lines(client.get("/v1/admin/trajectories.jsonl?kind=raw", headers=headers).text)
    assert len(raw) == 4 and all(line["internal"] is True for line in raw)


def test_knowledge_candidate_hidden_until_approved(client, session) -> None:
    from app import knowledge

    admin, headers = _api_user(client, session, "kb-admin@example.com", training=False, admin=True)
    author, _ = _api_user(client, session, "kb-author@example.com")
    task = start_task(session, author, "Изучи API Ласточки", {})
    saved = knowledge.execute(
        session, author, task.id, "knowledge_save",
        {"topic": "Ласточка API: тарифы", "fact": "До 1 кг — 410 руб., почта a@b.co", "source_url": "https://dev.lastochka.example/api"},
    )
    assert "на проверку" in saved.content
    assert knowledge.search(session, "Ласточка тарифы") == []
    assert knowledge.relevant_notes(session, "Ласточка тарифы") == []

    pending = client.get("/v1/admin/knowledge/candidates", headers=headers).json()["candidates"]
    assert len(pending) == 1 and "a@b.co" not in pending[0]["text"]
    quality = client.get("/v1/admin/quality", headers=headers).json()
    assert quality["knowledge_candidates_pending"] == 1
    denied = client.post(f"/v1/admin/knowledge/{pending[0]['id']}/review", headers=_api_user(client, session, "x@example.com")[1], json={"decision": "approve"})
    assert denied.status_code == 403
    approved = client.post(f"/v1/admin/knowledge/{pending[0]['id']}/review", headers=headers, json={"decision": "approve"})
    assert approved.status_code == 200 and approved.json()["approved_at"]
    session.expire_all()
    assert [n.topic for n in knowledge.search(session, "Ласточка тарифы")] == ["Ласточка API: тарифы"]
    assert knowledge.relevant_notes(session, "Ласточка тарифы")

    rejected = knowledge.save(session, author, "Ласточка API: лимиты", "60 запросов в минуту", "https://dev.lastochka.example/limits", candidate=True)
    session.commit()
    client.post(f"/v1/admin/knowledge/{rejected.id}/review", headers=headers, json={"decision": "reject"})
    session.expire_all()
    assert all(n.topic != "Ласточка API: лимиты" for n in knowledge.search(session, "Ласточка лимиты запросов"))
    assert client.get("/v1/admin/knowledge/candidates", headers=headers).json()["candidates"] == []


def test_withdraw_training_and_delete_history(client, session) -> None:
    from app.models import Credential, MemoryFact, Task, TaskFeedback, TaskSignal, TaskStep
    from app.trajectories import auto_finish

    user, headers = _api_user(client, session, "history@example.com")
    first = start_task(session, user, "Найди поставщика коробок", {})
    add_step(session, user, first.id, model_alias="heli-fast", reply="нашёл")
    auto_finish(session, first.id, "success", "нашёл")
    second = start_task(session, user, "Найди поставщика коробок дешевле", {})
    auto_finish(session, second.id, "success", "ещё")
    rate_task(session, user, second.id, rating=1)
    session.add(MemoryFact(user_id=user.id, text="склад в Казани", created_at=datetime.now(timezone.utc)))
    session.add(Credential(user_id=user.id, site="b24", username="u", nonce=b"n", ciphertext=b"c", created_at=datetime.now(timezone.utc)))
    session.commit()
    assert session.scalar(select(TaskSignal).where(TaskSignal.task_id == first.id)) is not None

    copy = client.delete("/v1/me/trajectories", headers=headers)
    assert copy.status_code == 200 and copy.json()["deleted"] == 2
    session.expire_all()
    assert session.scalar(select(Task).where(Task.id == first.id)) is not None

    client.put("/v1/me/consents", headers=headers, json={"consent_training": True})
    assert _doc(session, first.id) is not None
    client.put("/v1/me/consents", headers=headers, json={"consent_training": False})
    assert _doc(session, first.id) is None

    gone = client.delete("/v1/me/tasks", headers=headers)
    assert gone.status_code == 200
    assert gone.json()["deleted"]["tasks"] == 2 and gone.json()["deleted"]["signals"] >= 1
    session.expire_all()
    assert session.scalar(select(Task).where(Task.user_id == user.id)) is None
    assert session.scalar(select(TaskSignal).where(TaskSignal.user_id == user.id)) is None
    assert session.scalar(select(TaskStep)) is None and session.scalar(select(TaskFeedback)) is None
    assert session.scalar(select(MemoryFact).where(MemoryFact.user_id == user.id)) is not None
    assert session.scalar(select(Credential).where(Credential.user_id == user.id)) is not None


def test_closing_the_card_and_the_next_message_leave_signals(client, session) -> None:
    from app.models import Task, TaskSignal
    from app.trajectories import auto_finish

    user, headers = _api_user(client, session, "signals@example.com")

    def kinds(task_id) -> list[str]:
        session.expire_all()
        return [row.kind for row in session.scalars(select(TaskSignal).where(TaskSignal.task_id == task_id))]

    def task(text: str):
        task_id = uuid.UUID(client.post("/v1/tasks", headers=headers, json={"request": text, "context": {}}).json()["id"])
        auto_finish(session, task_id, "success", "ответ")
        session.commit()
        return task_id

    a = task("Собери отчёт по продажам за октябрь")
    assert client.post(f"/v1/tasks/{a}/dismiss", headers=headers).json()["noted"] is True
    assert client.post(f"/v1/tasks/{a}/dismiss", headers=headers).json()["noted"] is False
    assert kinds(a) == ["abandon"]

    b = task("Собери отчёт по продажам за октябрь")  # the same again: a retry of a
    assert "retry" in kinds(a)
    c = task("а за ноябрь отчёт по продажам?")  # short, same topic: reformulate of b
    assert kinds(b) == ["reformulate"]
    session.expire_all()
    assert session.get(Task, c).parent_task_id == b
    task("Напиши пост про новую кофемашину")  # another topic, c not rated
    assert kinds(c) == ["next_unrelated"]

    payload = _doc(session, a)
    assert {s["kind"] for s in payload["signals"]} == {"abandon", "retry"}

    # A rated card does not get abandon on ✕.
    d = task("Проверь остатки на складе")
    client.post(f"/v1/tasks/{d}/feedback", headers=headers, json={"rating": 1})
    assert client.post(f"/v1/tasks/{d}/dismiss", headers=headers).json()["noted"] is False


def test_run_journal_stop_reason_confirmations_and_continue(session) -> None:
    from app.agent import runs
    from app.models import Task, TaskSignal

    user = _user(session, training=True)
    task = start_task(session, user, "Заполни карточки товаров на сайте", {})
    add_step(session, user, task.id, tool_name="ask_confirmation", tool_args={"action": "сохранить"}, confirmation="approved")
    add_step(session, user, task.id, tool_name="ask_confirmation", tool_args={"action": "удалить"}, confirmation="rejected")
    task.context = {"checkpoint": {"request": task.request_text, "summary": "сделал 3 из 10", "stopped": "max_steps"}}
    session.commit()
    runs._close_task(session, task.id, "aborted", "Сделал 3 из 10", "max_steps")
    session.expire_all()
    task = session.get(Task, task.id)
    assert task.stopped == "max_steps" and task.finished_at is not None and task.duration_ms is not None
    kinds = sorted(row.kind for row in session.scalars(select(TaskSignal).where(TaskSignal.task_id == task.id)))
    assert kinds == ["confirm_approved", "confirm_rejected", "max_steps"]
    assert _doc(session, task.id)["stopped"] == "max_steps"
    # A rating later does not move the end of the run or duplicate the run signals.
    finish_task(session, user, task.id, "success", None)
    assert len(list(session.scalars(select(TaskSignal).where(TaskSignal.task_id == task.id)))) == 3

    follow = start_task(session, user, "продолжи", {})
    assert follow.parent_task_id == task.id
    assert "continue" in [row.kind for row in session.scalars(select(TaskSignal).where(TaskSignal.task_id == task.id))]


def test_followup_classifier_and_mail_detection() -> None:
    from datetime import timedelta

    from app.models import TaskStep
    from app.signals import classify_followup, has_mail, verify_results

    minute = timedelta(minutes=1)
    assert classify_followup("Найди аренду видеокарты", "продолжи", gap=minute, prev_rated=False) == "continue"
    assert classify_followup("Найди аренду видеокарты", "найди аренду видеокарты", gap=minute, prev_rated=True) == "retry"
    assert classify_followup("Найди аренду видеокарты в Москве", "а видеокарты подешевле?", gap=minute, prev_rated=False) == "reformulate"
    assert classify_followup("Найди аренду видеокарты", "Напиши пост про кофе", gap=minute, prev_rated=False) == "next_unrelated"
    assert classify_followup("Найди аренду видеокарты", "Напиши пост про кофе", gap=minute, prev_rated=True) is None
    assert classify_followup("Найди аренду видеокарты", "Напиши пост про кофе", gap=timedelta(hours=2), prev_rated=False) is None

    letter = TaskStep(position=1, tool_name="ask_confirmation", tool_args={"action": "Отправить письмо Ивану"})
    assert has_mail([letter]) and has_mail([TaskStep(position=1, tool_name="mail_send", tool_args={})])
    assert not has_mail([TaskStep(position=1, tool_name="browser_read", tool_args={})])
    write = TaskStep(position=1, tool_name="http_request", tool_args={"method": "POST", "url": "https://api.moysklad.ru/x"}, result_text="ok")
    read_fail = TaskStep(position=2, tool_name="moysklad_read", tool_args={}, result_text="Ошибка: 500", error="http_500")
    assert [k for k, _ in verify_results([write, read_fail])] == ["verify_fail"]


def test_admin_quality_and_feedback_fields(client, session) -> None:
    from app.trajectories import auto_finish

    admin, headers = _api_user(client, session, "q-admin@example.com", training=False, admin=True)
    user, user_headers = _api_user(client, session, "q-user@example.com")
    task = start_task(session, user, "Ответь на письмо клиента", {})
    add_step(session, user, task.id, tool_name="mail_send", tool_args={"to": "x"}, cost_rub="1.5")
    auto_finish(session, task.id, "aborted", "черновик", stopped="confirm")
    session.commit()
    client.post(f"/v1/tasks/{task.id}/feedback", headers=user_headers, json={"rating": -1, "reason_code": "slow", "mail_disposition": "declined"})
    assert client.get("/v1/admin/quality", headers=user_headers).status_code == 403
    quality = client.get("/v1/admin/quality?days=7", headers=headers).json()
    assert quality["tasks"] == 1 and quality["rated"] == 1 and quality["thumbs_down"] == 1
    assert quality["reason_codes"] == {"slow": 1} and quality["mail_disposition"] == {"declined": 1}
    assert quality["stopped"] == {"confirm": 1}
    assert quality["dataset"]["documents"] == 1 and quality["dataset"]["sft_ready"] == 0
    assert quality["hash_pepper_set"] is True
    feedback = client.get("/v1/admin/feedback", headers=headers).json()
    row = feedback["tasks"][0]
    assert row["stopped"] == "confirm" and row["feedback"]["reason_code"] == "slow"
    assert row["feedback"]["mail_disposition"] == "declined" and isinstance(row["signals"], list)
    assert feedback["totals"]["thumbs_down"] == 1
