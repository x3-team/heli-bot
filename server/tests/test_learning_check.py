import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.learning_check import learning_check, live_endpoints
from app.models import Task, TaskFeedback, Trajectory, User
from app.trajectories import auto_finish, rate_task, start_task


def _user(session, email, *, training=True, admin=False):
    user = User(
        email=email,
        consent_pdn=True,
        consent_training=training,
        is_admin=admin,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def test_learning_check_counts_and_never_writes(session, monkeypatch):
    monkeypatch.setenv("HELI_ADMIN_EMAILS", "")
    user = _user(session, "client@example.com")
    good = start_task(session, user, "Найди курс доллара, напиши на ivan@example.com", {})
    auto_finish(session, good.id, "success", "Курс 92,5 ₽", stopped=None)
    rate_task(session, user, good.id, rating=1)
    open_task = start_task(session, user, "Висит", {})
    open_task.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    session.commit()
    before = {
        "tasks": session.scalar(select(func.count()).select_from(Task)),
        "feedback": session.scalar(select(func.count()).select_from(TaskFeedback)),
        "trajectories": session.scalar(select(func.count()).select_from(Trajectory)),
    }

    report = learning_check(session)

    assert report["schema"]["task_signals_table"] is True
    assert set(report["schema"]["tasks_journal_columns"]) == {"finished_at", "stopped", "duration_ms", "parent_task_id"}
    assert report["journal_all"]["tasks"] == 2
    assert report["journal_all"]["with_finished_at"] == 1
    assert report["journal_all"]["never_closed"] == 1
    assert report["feedback"]["thumbs_up"] == 1
    assert report["trajectories"]["documents"] == 1
    assert report["export"]["sft"]["lines"] == 1
    assert report["export"]["sft"]["account_emails_found"] == 0
    assert report["export"]["sft"]["forbidden_keys"] == []
    assert report["quality"]["tasks"] == 2
    text = json.dumps(report, ensure_ascii=False, default=str)
    assert "client@example.com" not in text and "ivan@example.com" not in text and "Найди" not in text
    after = {
        "tasks": session.scalar(select(func.count()).select_from(Task)),
        "feedback": session.scalar(select(func.count()).select_from(TaskFeedback)),
        "trajectories": session.scalar(select(func.count()).select_from(Trajectory)),
    }
    assert before == after


def test_live_endpoints_as_admin(session, client, monkeypatch):
    monkeypatch.setenv("HELI_ADMIN_EMAILS", "")
    user = _user(session, "boss@example.com", admin=True)
    task = start_task(session, user, "Задача", {})
    auto_finish(session, task.id, "success", "Готово")
    rate_task(session, user, task.id, rating=1)
    session.commit()

    live = live_endpoints(session, "http://testserver", http=client)

    assert live["quality_status"] == 200
    assert live["quality_tasks"] == 1
    assert live["export_raw"]["status"] == 200 and live["export_raw"]["bad_json"] == 0
    assert live["export_sft"]["status"] == 200


def test_live_endpoints_without_admin_renders_in_process(session, client):
    user = _user(session, "client@example.com")
    task = start_task(session, user, "Задача", {})
    auto_finish(session, task.id, "success", "Готово")
    rate_task(session, user, task.id, rating=1)
    session.commit()

    live = live_endpoints(session, "http://testserver", http=client)

    assert live["no_token_status"] == 401
    assert live["skipped"] == "no_admin_user"
    assert live["in_process"]["quality_tasks"] == 1
    assert live["in_process"]["export_sft_lines"] == 1
    assert session.scalar(select(func.count()).select_from(User)) == 1


def test_email_places_name_the_key_not_the_value(session):
    user = _user(session, "client@example.com")
    task = start_task(session, user, "Сборка build@1.2.3", {})
    auto_finish(session, task.id, "success", "ok")
    session.commit()

    places = learning_check(session)["export"]["raw"]["email_places"]

    assert places == {"task (not_a_tld)": 1}


def test_lost_task_is_closed_at_the_next_start(session):
    from app.models import TaskSignal  # noqa: F401

    user = _user(session, "client@example.com")
    lost = start_task(session, user, "Задача до рестарта", {})
    fresh = start_task(session, user, "Только что", {})
    lost.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.commit()
    assert lost.outcome is None and fresh.outcome is None

    start_task(session, user, "Следующая задача", {})
    session.commit()

    session.refresh(lost)
    session.refresh(fresh)
    assert lost.outcome == "aborted" and lost.stopped == "lost" and lost.finished_at is not None
    assert session.scalar(select(Trajectory).where(Trajectory.task_id == lost.id)) is not None
    assert fresh.outcome is None  # younger than an hour: may still be running


def test_internal_account_is_marked_in_raw_and_never_counted_as_sft_ready(session, monkeypatch):
    from app.quality import quality_summary
    from app.trajectories import list_documents

    monkeypatch.setenv("HELI_OPEN_ACCESS_EMAIL", "owner@heli.local")
    user = _user(session, "owner@heli.local")
    task = start_task(session, user, "Задача", {})
    auto_finish(session, task.id, "success", "Готово")
    rate_task(session, user, task.id, rating=1)
    row = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
    row.payload = {k: v for k, v in row.payload.items() if k != "internal"}  # a document from before the flag
    session.commit()

    assert [doc["internal"] for doc in list_documents(session)] == [True]
    assert list_documents(session, exclude_internal=True) == []
    dataset = quality_summary(session)["dataset"]
    assert dataset["confirmed_good"] == 1 and dataset["sft_ready"] == 0


def test_revision_falls_back_to_the_deploy_file(tmp_path, monkeypatch):
    from app import trajectories

    for name in ("APP_REVISION", "GIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT"):
        monkeypatch.delenv(name, raising=False)
    file = tmp_path / "REVISION"
    file.write_text("10f8ab0 main 2026-11-07T12:58:08Z\n")
    monkeypatch.setattr(trajectories, "REVISION_FILE", file)
    assert trajectories.revision() == "10f8ab0"
    monkeypatch.setattr(trajectories, "REVISION_FILE", tmp_path / "missing")
    assert trajectories.revision() is None


def test_cyrillic_and_punycode_emails_are_masked():
    from app.masking import mask_text

    assert mask_text("пишите петя@почта.рф или info@кц-сервис.рф.") == "пишите [email] или [email]."
    assert mask_text("a.b@site.xn--p1ai") == "[email]"
    assert mask_text("сборка build@1.2.3") == "сборка build@1.2.3"


def test_old_documents_get_the_wider_email_mask_on_export(session):
    from app.trajectories import list_documents

    user = _user(session, "client@example.com")
    task = start_task(session, user, "Задача", {})
    auto_finish(session, task.id, "success", "Готово")
    row = session.scalar(select(Trajectory).where(Trajectory.task_id == task.id))
    row.payload = {**row.payload, "final_answer": "Отправил на петя@почта.рф"}  # written by the old mask
    session.commit()

    assert list_documents(session)[0]["final_answer"] == "Отправил на [email]"
