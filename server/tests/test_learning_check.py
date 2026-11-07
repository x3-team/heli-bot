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
    task = start_task(session, user, "Сборка build@1.2.3 и почта петя@почта.рф", {})
    auto_finish(session, task.id, "success", "ok")
    session.commit()

    places = learning_check(session)["export"]["raw"]["email_places"]

    assert places == {"task (not_a_tld)": 1, "task (non_ascii)": 1}
