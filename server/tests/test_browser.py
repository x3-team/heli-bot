from datetime import datetime, timedelta, timezone

import pytest

from app.auth import create_invite
from app.browser import (
    BrowserError,
    DockerDriver,
    MemoryDriver,
    container_spec,
    get_browser_driver,
    pool_start,
    pool_status,
    pool_stop,
)
from app.models import User


def _user(session, email="browser@example.com") -> User:
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


def test_container_is_limited_and_unprivileged() -> None:
    user_id = __import__("uuid").uuid4()
    spec = container_spec(user_id)
    assert spec.mem_limit == "1g"
    assert spec.nano_cpus == 1_000_000_000
    assert spec.privileged is False
    assert spec.cap_drop == ("ALL",)
    assert spec.network == "heli-browsers"
    assert spec.network != "host"
    assert spec.publish == ("6080/tcp", ("127.0.0.1", None))
    assert spec.volume == f"heli-profile-{user_id}"
    assert "no-new-privileges:true" in spec.security_opt


def test_one_browser_per_user_and_idle_stop_keeps_profile(session) -> None:
    user = _user(session)
    driver = MemoryDriver()
    started = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
    first = pool_start(session, user.id, driver, started)
    second = pool_start(session, user.id, driver, started + timedelta(minutes=1))
    assert first["status"] == "running"
    assert second["novnc_url"] == "http://127.0.0.1:6080/vnc.html"
    assert len(driver.specs) == 1
    assert len(driver.containers) == 1

    view = pool_status(session, user.id, driver, started + timedelta(minutes=12))
    assert view["status"] == "stopped"
    assert view["novnc_url"] is None
    assert driver.containers == {}
    assert f"heli-profile-{user.id}" in driver.volumes

    again = pool_start(session, user.id, driver, started + timedelta(minutes=13))
    assert again["status"] == "running"
    assert f"heli-profile-{user.id}" in driver.volumes


def test_pool_stops_on_request_and_refuses_when_full(session, monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_MAX", "1")
    first = _user(session, "one@example.com")
    second = _user(session, "two@example.com")
    driver = MemoryDriver()
    pool_start(session, first.id, driver)
    with pytest.raises(BrowserError) as caught:
        pool_start(session, second.id, driver)
    assert caught.value.code == "browser_busy"
    stopped = pool_stop(session, first.id, driver)
    assert stopped["status"] == "stopped"
    assert f"heli-profile-{first.id}" in driver.volumes
    opened = pool_start(session, second.id, driver)
    assert opened["status"] == "running"


class _FakeContainer:
    id = "container-1"
    ports = {"6080/tcp": [{"HostPort": "49152"}]}

    def reload(self) -> None:
        return None

    def stop(self, timeout: int = 5) -> None:
        return None

    def remove(self) -> None:
        return None


class _FakeDocker:
    def __init__(self) -> None:
        self.kwargs = None
        self.removed_volumes: list[str] = []
        self.networks = self
        self.containers = self

    def get(self, name: str):
        if name == "heli-browsers":
            raise KeyError(name)
        return _FakeContainer()

    def create(self, name: str, **kwargs):
        assert name == "heli-browsers"
        assert kwargs.get("internal") is False

    def run(self, image, **kwargs):
        self.kwargs = {"image": image, **kwargs}
        return _FakeContainer()

    def ping(self) -> None:
        return None


def test_docker_driver_publishes_novnc_on_localhost_only() -> None:
    fake = _FakeDocker()
    started = DockerDriver(lambda: fake).start(__import__("uuid").uuid4())
    assert started.novnc_url == "http://127.0.0.1:49152/vnc.html"
    assert fake.kwargs["privileged"] is False
    assert fake.kwargs["mem_limit"] == "1g"
    assert fake.kwargs["nano_cpus"] == 1_000_000_000
    assert fake.kwargs["cap_drop"] == ["ALL"]
    assert fake.kwargs["network"] == "heli-browsers"
    assert fake.kwargs["ports"]["6080/tcp"][0] == "127.0.0.1"
    assert "/var/run/docker.sock" not in str(fake.kwargs["volumes"])
    assert fake.removed_volumes == []


def test_missing_docker_is_unavailable() -> None:
    def down():
        raise BrowserError("browser_unavailable")

    with pytest.raises(BrowserError) as caught:
        DockerDriver(down).start(__import__("uuid").uuid4())
    assert caught.value.code == "browser_unavailable"


def _login(client, session, email: str) -> dict[str, str]:
    invite = create_invite(session)
    session.commit()
    issued = client.post("/v1/auth/request-code", json={"email": email, "invite_code": invite})
    assert issued.status_code == 202
    code = client.app.state.mail_sink[-1]
    token = client.post("/v1/auth/verify", json={"email": email, "code": code}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_browser_routes(client, session) -> None:
    headers = _login(client, session, "pool@example.com")
    driver = MemoryDriver()
    client.app.dependency_overrides[get_browser_driver] = lambda: driver
    assert client.get("/v1/browser", headers=headers).json()["status"] == "stopped"
    started = client.post("/v1/browser/start", headers=headers)
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert started.json()["idle_minutes"] == 10
    stopped = client.post("/v1/browser/stop", headers=headers)
    assert stopped.json()["status"] == "stopped"

    class Down:
        def start(self, user_id):
            raise BrowserError("browser_unavailable")

        def stop(self, user_id, container_id):
            raise BrowserError("browser_unavailable")

    client.app.dependency_overrides[get_browser_driver] = lambda: Down()
    missing = client.post("/v1/browser/start", headers=headers)
    assert missing.status_code == 503
    assert missing.json()["detail"] == "browser_unavailable"
