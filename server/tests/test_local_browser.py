from datetime import datetime, timedelta, timezone

import pytest

from app.auth import create_invite
from app.browser import BrowserError, MemoryDriver, attach_local, pool_start, pool_status
from app.models import User


def _user(session) -> User:
    user = User(
        email="local@example.com",
        consent_pdn=True,
        consent_training=False,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    return user


def test_local_attach_and_idle_stop(session) -> None:
    user = _user(session)
    started = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
    view = attach_local(session, user.id, "chrome", 9222, now=started)
    assert view["status"] == "running"
    assert view["mode"] == "local"
    assert view["local_browser"] == "chrome"
    assert view["local_port"] == 9222
    assert view["novnc_url"] is None
    stopped = pool_status(session, user.id, MemoryDriver(), started + timedelta(minutes=12))
    assert stopped["status"] == "stopped"


def test_prefer_local_requires_desktop(session) -> None:
    user = _user(session)
    with pytest.raises(BrowserError) as caught:
        pool_start(session, user.id, MemoryDriver(), prefer_local=True)
    assert caught.value.code == "local_browser_required"


def test_local_http(client, session) -> None:
    invite = create_invite(session)
    session.commit()
    issued = client.post(
        "/v1/auth/request-code",
        json={"email": "cdp@example.com", "invite_code": invite},
    )
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": "cdp@example.com", "code": code}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    attached = client.post(
        "/v1/browser/local/attach",
        headers=headers,
        json={"browser": "edge", "port": 9223},
    )
    assert attached.status_code == 200
    body = attached.json()
    assert body["mode"] == "local"
    assert body["local_browser"] == "edge"
    need = client.post("/v1/browser/start?prefer_local=true", headers=headers)
    assert need.status_code == 200
    assert need.json()["mode"] == "local"
    cloud = client.post("/v1/browser/start?prefer_local=false", headers=headers)
    assert cloud.status_code == 200
    assert cloud.json()["mode"] == "local"
