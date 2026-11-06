"""GA4 (ga4_read, ga4_send_event), hit-collection endpoints and the analytics knowledge.

The rule under test: reports are read on any property; a Measurement Protocol hit goes only to a training
Measurement ID, and anything else is refused before a credential is read or a request is made.
"""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.agent import credentials, google_analytics as ga, http_tool
from app.models import User
from app.vault import store_credential
from app.web import WebError, check_url, is_tracking_hit

KEY = base64.b64encode(b"k" * 32).decode()
TRAIN_ID = "G-TRAIN0001"
FOREIGN_ID = "G-K7PROD42X"
API_SECRET = "mp-secret-abcdef123456"
GA_TOKEN = "ya29.ga-read-token-0123456789"
PUBLIC_DNS = lambda host, port: [(0, 0, 0, "", ("142.250.74.110", port))]  # noqa: E731


def _user(session) -> User:
    user = User(email="ga@example.com", consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def _never(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
    raise AssertionError(f"request must not be sent: {request.method} {request.url}")


PING = {"events": [{"name": "heli_train_ping", "params": {"source": "heli"}}]}


# --- Measurement Protocol: training IDs only ---------------------------------------


def test_no_training_ids_are_built_in(monkeypatch) -> None:
    monkeypatch.delenv("HELI_GA4_SANDBOX_IDS", raising=False)
    assert ga.env_sandbox_ids() == set()


@pytest.mark.parametrize("mid", [FOREIGN_ID, "g-k7prod42x", "G-XXXXXXXX"])
def test_hit_to_an_unknown_id_is_refused_without_network(session, monkeypatch, mid) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("HELI_GA4_SANDBOX_IDS", TRAIN_ID)
    user = _user(session)
    # Even with its API secret saved, a live ID gets no hit.
    store_credential(session, user, "www.google-analytics.com", FOREIGN_ID, API_SECRET)
    result = ga.execute(session, user, None, "ga4_send_event", {"measurement_id": mid, **PING},
                        transport=httpx.MockTransport(_never))
    assert result.error == "write_forbidden"
    assert "учебн" in result.content and API_SECRET not in result.content


def test_refusal_happens_before_the_vault_is_read(monkeypatch) -> None:
    def boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("vault read")

    monkeypatch.setattr(ga, "api_secret_for", boom)
    with pytest.raises(ga.GaError) as exc:
        ga.check_send({"measurement_id": FOREIGN_ID, **PING}, {TRAIN_ID})
    assert exc.value.code == "write_forbidden"


def test_training_id_from_env_sends_ping_and_hides_the_secret(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("HELI_GA4_SANDBOX_IDS", f" {TRAIN_ID.lower()} , junk")
    user = _user(session)
    store_credential(session, user, "ga4-sandbox", TRAIN_ID, API_SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    result = ga.execute(session, user, None, "ga4_send_event", {"measurement_id": TRAIN_ID, **PING},
                        transport=httpx.MockTransport(handler))
    assert result.error is None and "heli_train_ping" in result.content and API_SECRET not in result.content
    req = seen[0]
    assert req.method == "POST" and str(req.url).startswith(ga.MP_URL)
    assert req.url.params["measurement_id"] == TRAIN_ID and req.url.params["api_secret"] == API_SECRET
    body = json.loads(req.content)
    assert body["events"][0]["name"] == "heli_train_ping" and body["client_id"].startswith("heli-train.")


def test_training_id_from_vault_and_validate_uses_debug_endpoint(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.delenv("HELI_GA4_SANDBOX_IDS", raising=False)
    user = _user(session)
    store_credential(session, user, "ga4-sandbox", TRAIN_ID, API_SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"validationMessages": []})

    result = ga.execute(session, user, None, "ga4_send_event",
                        {"measurement_id": TRAIN_ID, "validate": True, **PING}, transport=httpx.MockTransport(handler))
    assert result.error is None and "без записи" in result.content
    assert str(seen[0].url).startswith(ga.MP_DEBUG_URL)


def test_training_id_without_secret_explains_and_sends_nothing(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("HELI_GA4_SANDBOX_IDS", TRAIN_ID)
    user = _user(session)
    result = ga.execute(session, user, None, "ga4_send_event", {"measurement_id": TRAIN_ID, **PING},
                        transport=httpx.MockTransport(_never))
    assert result.error == "no_credential" and "ga4-sandbox" in result.content


@pytest.mark.parametrize("events", [[], [{"name": "ga_session"}], [{"name": "1bad"}], [{"name": "x" * 41}], [{}] * 26])
def test_bad_events_are_refused(events) -> None:
    with pytest.raises(ga.GaError) as exc:
        ga.check_send({"measurement_id": TRAIN_ID, "events": events}, {TRAIN_ID})
    assert exc.value.code in {"bad_events"}


def test_agent_cannot_whitelist_an_id_through_save_credential(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    result = credentials.execute(session, user, None, "save_credential",
                                 {"site": "ga4-sandbox", "username": FOREIGN_ID, "password": API_SECRET})
    assert result.error == "write_forbidden"
    assert ga.vault_sandbox_ids([]) == set()


# --- reading -----------------------------------------------------------------------


def test_run_report_is_a_post_read_with_bearer_and_token_hidden(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    store_credential(session, user, "analytics.google.com", "token", GA_TOKEN)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": "generate_lead"}],
                                                   "metricValues": [{"value": "23"}]}], "echo": GA_TOKEN})

    result = ga.execute(session, user, None, "ga4_read", {
        "report": "run_report", "property": "properties/400000001",
        "params": {"dimensions": [{"name": "eventName"}], "metrics": [{"name": "keyEvents"}]},
    }, transport=httpx.MockTransport(handler))
    assert result.error is None and "generate_lead" in result.content and GA_TOKEN not in result.content
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1beta/properties/400000001:runReport"
    assert req.headers["Authorization"] == f"Bearer {GA_TOKEN}"
    assert json.loads(req.content)["dateRanges"]


def test_admin_api_is_get_only() -> None:
    for report in ("key_events", "data_streams", "account_summaries", "metadata"):
        method, url, body = ga.build_read({"report": report, "property": "1"})
        assert method == "GET" and body is None, report
    with pytest.raises(ga.GaError):
        ga.build_read({"report": "update_key_event", "property": "1"})
    with pytest.raises(ga.GaError):
        ga.call_read("PATCH", ga.ADMIN_API + "properties/1/keyEvents/2", {}, "t", httpx.MockTransport(_never))
    with pytest.raises(ga.GaError):
        ga.call_read("POST", ga.ADMIN_API + "properties/1/keyEvents", {}, "t", httpx.MockTransport(_never))


def test_service_account_key_gets_a_readonly_token(session, monkeypatch) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    ga._SA_CACHE.clear()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    user = _user(session)
    store_credential(session, user, "analytics.google.com", "service_account",
                     json.dumps({"client_email": "heli@train.iam.gserviceaccount.com", "private_key": pem}))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            claims = jwt.decode(form["assertion"], private.public_key(), algorithms=["RS256"], audience=ga.TOKEN_URL)
            assert claims["scope"] == ga.READ_SCOPE
            return httpx.Response(200, json={"access_token": GA_TOKEN, "expires_in": 3600})
        return httpx.Response(200, json={"keyEvents": [{"eventName": "generate_lead"}]})

    result = ga.execute(session, user, None, "ga4_read", {"report": "key_events", "property": "400000001"},
                        transport=httpx.MockTransport(handler))
    assert result.error is None and "generate_lead" in result.content
    assert seen[1].method == "GET" and seen[1].headers["Authorization"] == f"Bearer {GA_TOKEN}"
    assert "BEGIN PRIVATE KEY" not in result.content


def test_read_without_credential_explains_how_to_add_it(session, monkeypatch) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    user = _user(session)
    result = ga.execute(session, user, None, "ga4_read", {"report": "account_summaries"},
                        transport=httpx.MockTransport(_never))
    assert result.error == "no_credential" and "service_account" in result.content


# --- http_request and open_url: hit endpoints are closed for every method -------------


@pytest.mark.parametrize("url", [
    "https://www.google-analytics.com/mp/collect?measurement_id=G-K7PROD42X&api_secret=x",
    "https://www.google-analytics.com/debug/mp/collect?measurement_id=G-K7PROD42X",
    "https://region1.google-analytics.com/g/collect?tid=G-K7PROD42X&en=page_view",
    "https://analytics.google.com/g/collect?tid=G-K7PROD42X",
    "https://mc.yandex.ru/watch/99999999?page-url=https://example.com",
    "https://mc.yandex.com/collect?tid=99999999",
    "https://mc.webvisor.org/watch/99999999",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_http_request_never_sends_analytics_hits(session, monkeypatch, url, method) -> None:
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    # A sandbox host list that names the collector does not open it either.
    monkeypatch.setenv("HELI_HTTP_SANDBOX_HOSTS", "www.google-analytics.com,mc.yandex.ru")
    user = _user(session)
    result = http_tool.execute(session, user, None, "http_request", {"method": method, "url": url, "json": PING},
                               transport=httpx.MockTransport(_never), resolver=PUBLIC_DNS)
    assert result.error == "tracking_forbidden"
    assert "ga4_send_event" in result.content


def test_open_url_refuses_hit_endpoints_before_dns() -> None:
    def no_dns(*_a):  # pragma: no cover - must not be reached
        raise AssertionError("dns")

    with pytest.raises(WebError) as exc:
        check_url("https://www.google-analytics.com/g/collect?tid=G-K7PROD42X", no_dns)
    assert exc.value.code == "tracking_hit"
    assert not is_tracking_hit("analytics.google.com", "/analytics/web/")
    assert not is_tracking_hit("api-metrika.yandex.net", "/stat/v1/data")


def test_http_policy_for_ga4_and_metrika_apis() -> None:
    run = "https://analyticsdata.googleapis.com/v1beta/properties/400000001:runReport"
    assert http_tool.check_policy("POST", run, {}) == "read"
    assert http_tool.check_policy("GET", "https://api-metrika.yandex.net/stat/v1/data?ids=99999999", None) == "read"
    for method, url in [
        ("PATCH", "https://analyticsadmin.googleapis.com/v1beta/properties/400000001/keyEvents/1"),
        ("POST", "https://analyticsadmin.googleapis.com/v1beta/properties/400000001/keyEvents"),
        ("DELETE", "https://analyticsadmin.googleapis.com/v1beta/properties/400000001/keyEvents/1"),
        ("POST", "https://api-metrika.yandex.net/management/v1/counter/99999999/goals"),
        ("PUT", "https://api-metrika.yandex.net/management/v1/counter/99999999"),
    ]:
        with pytest.raises(http_tool.HttpToolError):
            http_tool.check_policy(method, url, {})


# --- knowledge and playbook --------------------------------------------------------


def test_knowledge_explains_key_event_with_a_google_source(session) -> None:
    from app.knowledge import relevant_notes

    notes = relevant_notes(session, "Что такое key event в GA4 и чем ключевое событие отличается от цели Метрики?")
    assert notes and "support.google.com/analytics" in "\n".join(notes)
    joined = "\n".join(relevant_notes(session, "Measurement Protocol тестовый хит на боевой сайт"))
    assert "только на учебный" in joined and "Не предлагать" in joined


def test_direct_conversion_rule_still_comes_first(session) -> None:
    from app.knowledge import relevant_notes

    notes = relevant_notes(session, "Какую стратегию поставить в Директе, оплата за конверсии?")
    assert notes and "10 конверсий в неделю" in notes[0]


def test_ga4_playbook_keeps_the_rules() -> None:
    from app.skills import guide_for

    guide = guide_for("Посмотри ключевые события в GA4 и отправь тестовый hit")
    assert guide
    assert "учебный Measurement ID" in guide and "≥10 конверсий в неделю" in guide
    assert "support.google.com/analytics" in guide and "yandex.ru/support/metrica" in guide
