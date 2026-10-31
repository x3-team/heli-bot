import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.models import User
from app.notify import notify
from app.quota import record_charge


def _user(session) -> User:
    user = User(
        email="telegram@example.com",
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def test_skip_without_a_token(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    assert notify("Готово") is False


def test_quota_alert_is_sent_and_the_token_is_not_logged(session, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-bot")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "100")
    sent: list[str] = []

    def fake_post(url, json, timeout):
        if "unit-test-bot" not in url or json.get("chat_id") != "100":
            raise AssertionError("unexpected notification target")
        sent.append(json["text"])

        class Response:
            def raise_for_status(self) -> None:
                return None

        return Response()

    monkeypatch.setattr("app.notify.httpx.post", fake_post)
    record_charge(session, _user(session), Decimal("250"))
    assert "Лимит недели достиг 50%." in sent
    assert "Лимит недели достиг 80%." in sent
    assert all("руб" not in text for text in sent)
    if "unit-test-bot" in caplog.text:
        raise AssertionError("bot token appeared in logs")
