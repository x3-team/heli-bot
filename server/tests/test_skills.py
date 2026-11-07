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


def test_moysklad_read_is_get_only_and_compact() -> None:
    import httpx

    from app.agent import moysklad

    path, params = moysklad.build_request({"resource": "stock", "below_minimum": True, "limit": 500})
    assert path == "report/stock/all" and params == {"limit": 50, "filter": "stockMode=underMinimum"}
    path, params = moysklad.build_request({"resource": "supply", "id": "0b1c2d3e-0000-4000-8000-000000000001"})
    assert path.endswith("/0b1c2d3e-0000-4000-8000-000000000001") and "positions" in params["expand"]
    for bad in ({"resource": "webhook"}, {"resource": "product", "id": "../../x"}):
        try:
            moysklad.build_request(bad)
            raise AssertionError("must refuse")
        except moysklad.MsError:
            pass

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"meta": {"size": 1}, "rows": [
            {"meta": {"href": "x"}, "id": "1", "name": "Перчатки", "article": "GL-M-100", "stock": 3,
             "salePrice": 12550, "folder": {"name": "Склад"}}]})

    data = moysklad.fetch("entity/product", {"filter": "article=GL-M-100"}, "Basic x", httpx.MockTransport(handler))
    text = moysklad.format_result(data)
    assert seen[0].method == "GET" and seen[0].url.host == "api.moysklad.ru"
    assert '"article": "GL-M-100"' in text and '"salePrice": 125.5' in text and "href" not in text
    assert moysklad.auth_header("token", "abc") == "Bearer abc"


def test_bitrix24_read_allows_only_reading_methods() -> None:
    from app.agent import bitrix

    assert bitrix.build_call({"method": "crm.deal.list", "params": {"select": ["ID"]}}) == ("crm.deal.list", {"select": ["ID"]})
    for method in ("crm.deal.add", "crm.deal.update", "crm.deal.delete", "tasks.task.add", "batch"):
        with pytest.raises(bitrix.BxError):
            bitrix.build_call({"method": method})


def test_bitrix24_webhook_goes_only_to_the_portal_host() -> None:
    from app.agent import bitrix

    assert bitrix.webhook_base("romashka.bitrix24.ru", "https://evil.example/rest/1/abc123/") == (
        "https://romashka.bitrix24.ru/rest/1/abc123/"
    )
    assert bitrix.webhook_base("https://romashka.bitrix24.ru/", "1/abc123") == "https://romashka.bitrix24.ru/rest/1/abc123/"
    with pytest.raises(bitrix.BxError):
        bitrix.webhook_base("romashka.bitrix24.ru", "not a webhook")


def test_bitrix24_read_uses_the_webhook_and_keeps_it_out_of_logins(session, monkeypatch) -> None:
    import httpx

    from app.agent import bitrix

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session, "bx@example.com")
    store_credential(session, user, "romashka.bitrix24.ru", "ivan", SECRET)
    store_credential(session, user, "romashka.bitrix24.ru", "webhook", "https://romashka.bitrix24.ru/rest/7/qwe789/")
    session.flush()
    # The sign-in form gets the login, never the webhook.
    assert credential_for(session, user, url="https://romashka.bitrix24.ru/")["username"] == "ivan"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"result": [{"ID": "5", "TITLE": "Окна", "OPPORTUNITY": "120000.00", "X": ""}], "total": 1})

    result = bitrix.execute(
        session, user, None, "bitrix24_read",
        {"method": "crm.deal.list", "params": {"filter": {"STAGE_ID": "NEW"}}},
        transport=httpx.MockTransport(handler),
    )
    assert seen == [("POST", "https://romashka.bitrix24.ru/rest/7/qwe789/crm.deal.list.json", {"filter": {"STAGE_ID": "NEW"}})]
    assert result.error is None and "Окна" in result.content and "Найдено 1" in result.content
    assert "qwe789" not in result.content


def test_bitrix24_read_without_webhook_explains_how_to_add_one(session, monkeypatch) -> None:
    from app.agent import bitrix

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session, "nobx@example.com")
    result = bitrix.execute(session, user, None, "bitrix24_read", {"method": "crm.deal.list"})
    assert result.error == "no_credential" and "Входящий вебхук" in result.content


def test_bitrix24_write_methods_are_separate_and_never_delete() -> None:
    from app.agent import bitrix

    assert bitrix.build_call({"method": "tasks.task.add", "params": {"fields": {"TITLE": "x"}}}, write=True)[0] == "tasks.task.add"
    for method in ("crm.deal.add", "crm.deal.update", "crm.timeline.comment.add", "task.commentitem.add", "tasks.task.complete"):
        assert bitrix.build_call({"method": method}, write=True)[0] == method
    for method in ("crm.deal.delete", "tasks.task.delete", "crm.contact.delete", "batch", "crm.deal.list"):
        with pytest.raises(bitrix.BxError):
            bitrix.build_call({"method": method}, write=True)
    assert not bitrix.WRITE_METHODS & bitrix.METHODS
    assert not any(m.endswith(".delete") for m in bitrix.WRITE_METHODS)


def test_bitrix24_write_only_on_a_training_portal(session, monkeypatch) -> None:
    import httpx

    from app.agent import bitrix

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("HELI_BITRIX_SANDBOX_HOSTS", "demo.bitrix24.ru")
    user = _user(session, "bxw@example.com")
    store_credential(session, user, "romashka.bitrix24.ru", "webhook", "https://romashka.bitrix24.ru/rest/7/qwe789/")
    store_credential(session, user, "demo.bitrix24.ru", "webhook", "https://demo.bitrix24.ru/rest/1/abc123/")
    session.flush()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"result": {"task": {"id": "42", "title": "Пересчитать"}}})

    args = {"method": "tasks.task.add", "params": {"fields": {"TITLE": "Пересчитать", "RESPONSIBLE_ID": 1}}}
    live = bitrix.execute(session, user, None, "bitrix24_write", {**args, "portal": "romashka.bitrix24.ru"},
                          transport=httpx.MockTransport(handler))
    assert live.error == "confirmation_required" and "не подтверждена" in live.content and seen == []
    demo = bitrix.execute(session, user, None, "bitrix24_write", {**args, "portal": "demo.bitrix24.ru"},
                          transport=httpx.MockTransport(handler))
    assert demo.error is None and '"id": "42"' in demo.content and "bitrix24_read" in demo.content
    assert seen == ["https://demo.bitrix24.ru/rest/1/abc123/tasks.task.add.json"]
    # The read tool still refuses a write, so a live portal cannot be changed through it.
    refused = bitrix.execute(session, user, None, "bitrix24_read", {**args, "portal": "romashka.bitrix24.ru"})
    assert refused.error == "bad_method" and "bitrix24_write" in refused.content
    assert bitrix.is_sandbox("https://b24-lvsc4f.bitrix24.ru/crm/") and not bitrix.is_sandbox("romashka.bitrix24.ru")


def test_bitrix24_webhook_goes_before_the_sign_in(session, monkeypatch) -> None:
    import uuid as _uuid

    from app import cloud_browser as cb
    from app.agent import credentials

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session, "bxc@example.com")
    store_credential(session, user, "b24-lvsc4f.bitrix24.ru", "diggo@example.com", SECRET)
    store_credential(session, user, "b24-lvsc4f.bitrix24.ru", "webhook", "https://b24-lvsc4f.bitrix24.ru/rest/1/abc123/")
    session.flush()
    found = credentials.execute(session, user, None, "find_credential", {"site": "b24-lvsc4f.bitrix24.ru"})
    assert found.content.startswith("Для b24-lvsc4f.bitrix24.ru в «Доступах» есть вебхук")
    assert "bitrix24_write" in found.content and "diggo@example.com" in found.content and "abc123" not in found.content

    class Pool:
        def reserve(self, *a, **kw):
            return None

        def call(self, *a, **kw):
            return cb.Result("Вход", None, None)

        def captcha(self, user_id, halted=None):
            return True

        def url(self, user_id):
            return "https://auth2.bitrix24.net/oauth/?captcha"

        def frame_version(self, user_id):
            return 1

    asked = []
    monkeypatch.setattr(cb, "pool", lambda: Pool())
    monkeypatch.setattr(cb, "ask_for_captcha", lambda *a, **kw: asked.append(a) or False)
    result = cb.execute(session, user, _uuid.uuid4(), "browser_open", {"url": "https://b24-lvsc4f.bitrix24.ru/"})
    assert result.error == "captcha_use_rest" and "bitrix24_read" in result.content
    assert asked == []
    assert cb._bitrix_portal("https://auth2.bitrix24.net/x", "") == ""
    assert cb._bitrix_portal("https://avito.ru/", "") is None


def test_bitrix24_webhook_without_rights_says_insufficient_scope() -> None:
    import httpx

    from app.agent import bitrix

    def scope(request):
        return httpx.Response(401, json={"error": "insufficient_scope", "error_description": "no tasks"})

    def bad(request):
        return httpx.Response(401, json={"error": "invalid_token"})

    with pytest.raises(bitrix.BxError) as no_rights:
        bitrix.fetch("https://b24-x.bitrix24.ru/rest/1/abc/", "tasks.task.list", {}, httpx.MockTransport(scope))
    assert no_rights.value.code == "insufficient_scope"
    assert "запасной путь" in no_rights.value.text
    with pytest.raises(bitrix.BxError) as wrong:
        bitrix.fetch("https://b24-x.bitrix24.ru/rest/1/abc/", "crm.deal.list", {}, httpx.MockTransport(bad))
    assert wrong.value.code == "auth_failed"
