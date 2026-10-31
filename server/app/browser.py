"""Per-user cloud browser. One container, a persistent profile, idle stop after 10 minutes."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import BrowserSlot

log = logging.getLogger(__name__)

IDLE = timedelta(minutes=10)
NETWORK = "heli-browsers"


class BrowserError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContainerSpec:
    image: str
    name: str
    volume: str
    mem_limit: str
    nano_cpus: int
    privileged: bool
    cap_drop: tuple[str, ...]
    network: str
    security_opt: tuple[str, ...]
    publish: tuple[str, tuple[str, None]]


@dataclass(frozen=True)
class Started:
    container_id: str
    novnc_url: str


def container_spec(user_id: uuid.UUID) -> ContainerSpec:
    safe = str(user_id)
    return ContainerSpec(
        image=get_settings().browser_image,
        name=f"heli-browser-{safe}",
        volume=f"heli-profile-{safe}",
        mem_limit="1g",
        nano_cpus=1_000_000_000,
        privileged=False,
        cap_drop=("ALL",),
        network=NETWORK,
        security_opt=("no-new-privileges:true",),
        publish=("6080/tcp", ("127.0.0.1", None)),
    )


class MemoryDriver:
    """In-process stand-in. Tests use it; production uses DockerDriver."""

    def __init__(self) -> None:
        self.containers: dict[str, str] = {}
        self.volumes: set[str] = set()
        self.specs: list[ContainerSpec] = []

    def start(self, user_id: uuid.UUID) -> Started:
        spec = container_spec(user_id)
        self.specs.append(spec)
        self.volumes.add(spec.volume)
        container_id = f"mem-{user_id}"
        self.containers[str(user_id)] = container_id
        return Started(container_id=container_id, novnc_url="http://127.0.0.1:6080/vnc.html")

    def stop(self, user_id: uuid.UUID, container_id: str | None) -> None:
        self.containers.pop(str(user_id), None)


class DockerDriver:
    def __init__(self, client_factory=None) -> None:
        self._client_factory = client_factory or default_docker_client

    def start(self, user_id: uuid.UUID) -> Started:
        client = self._client_factory()
        spec = container_spec(user_id)
        _ensure_network(client, spec.network)
        try:
            container = client.containers.run(
                spec.image,
                detach=True,
                name=spec.name,
                mem_limit=spec.mem_limit,
                nano_cpus=spec.nano_cpus,
                privileged=spec.privileged,
                cap_drop=list(spec.cap_drop),
                network=spec.network,
                security_opt=list(spec.security_opt),
                volumes={spec.volume: {"bind": "/profile", "mode": "rw"}},
                ports={spec.publish[0]: spec.publish[1]},
                labels={"heli.role": "browser", "heli.user": str(user_id)},
            )
            container.reload()
            binding = container.ports[spec.publish[0]][0]["HostPort"]
        except BrowserError:
            raise
        except Exception:
            log.info("browser container failed to start")
            raise BrowserError("browser_unavailable") from None
        return Started(container_id=container.id, novnc_url=f"http://127.0.0.1:{binding}/vnc.html")

    def stop(self, user_id: uuid.UUID, container_id: str | None) -> None:
        client = self._client_factory()
        try:
            container = client.containers.get(container_id or container_spec(user_id).name)
            container.stop(timeout=5)
            container.remove()
        except BrowserError:
            raise
        except Exception:
            log.info("browser container stop failed")
            raise BrowserError("browser_unavailable") from None


def get_browser_driver() -> DockerDriver:
    return DockerDriver()


def default_docker_client():
    try:
        import docker
    except ImportError:
        raise BrowserError("browser_unavailable") from None
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        raise BrowserError("browser_unavailable") from None
    return client


def pool_status(session: Session, user_id: uuid.UUID, driver, now: datetime | None = None) -> dict:
    reap_idle(session, driver, now)
    slot = session.get(BrowserSlot, user_id)
    if slot is not None and slot.status == "running":
        slot.last_activity = _aware(now)
        session.flush()
    return _public(slot)


def pool_start(session: Session, user_id: uuid.UUID, driver, now: datetime | None = None) -> dict:
    reap_idle(session, driver, now)
    slot = session.get(BrowserSlot, user_id)
    if slot is not None and slot.status == "running":
        slot.last_activity = _aware(now)
        session.flush()
        return _public(slot)
    running = session.scalar(
        select(func.count()).select_from(BrowserSlot).where(BrowserSlot.status == "running")
    )
    if int(running or 0) >= get_settings().browser_max:
        raise BrowserError("browser_busy")
    started = driver.start(user_id)
    moment = _aware(now)
    if slot is None:
        slot = BrowserSlot(
            user_id=user_id,
            container_id=started.container_id,
            novnc_url=started.novnc_url,
            status="running",
            last_activity=moment,
            created_at=moment,
        )
        session.add(slot)
    else:
        slot.container_id = started.container_id
        slot.novnc_url = started.novnc_url
        slot.status = "running"
        slot.last_activity = moment
    session.flush()
    return _public(slot)


def pool_stop(session: Session, user_id: uuid.UUID, driver) -> dict:
    slot = session.get(BrowserSlot, user_id)
    if slot is not None and slot.status == "running":
        driver.stop(user_id, slot.container_id)
        slot.status = "stopped"
        slot.container_id = None
        slot.novnc_url = None
        session.flush()
    return _public(slot)


def reap_idle(session: Session, driver, now: datetime | None = None) -> int:
    moment = _aware(now)
    cutoff = moment - IDLE
    stopped = 0
    slots = list(session.scalars(select(BrowserSlot).where(BrowserSlot.status == "running")))
    for slot in slots:
        activity = slot.last_activity
        if activity.tzinfo is None:
            activity = activity.replace(tzinfo=timezone.utc)
        if activity >= cutoff:
            continue
        try:
            driver.stop(slot.user_id, slot.container_id)
        except BrowserError:
            log.info("browser idle stop skipped")
            continue
        slot.status = "stopped"
        slot.container_id = None
        slot.novnc_url = None
        stopped += 1
    if stopped:
        session.flush()
    return stopped


def _public(slot: BrowserSlot | None) -> dict:
    if slot is None or slot.status != "running":
        return {"status": "stopped", "novnc_url": None, "idle_minutes": 10}
    return {"status": "running", "novnc_url": slot.novnc_url, "idle_minutes": 10}


def _ensure_network(client, name: str) -> None:
    try:
        client.networks.get(name)
    except Exception:
        client.networks.create(name, internal=False, check_duplicate=True)


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def install_reaper(driver_factory=None) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST") or get_settings().app_env == "test":
        return
    factory = driver_factory or DockerDriver

    def loop() -> None:
        from app.db import get_session_factory

        while True:
            threading.Event().wait(60)
            try:
                session = get_session_factory()()
                try:
                    reap_idle(session, factory())
                    session.commit()
                finally:
                    session.close()
            except Exception:
                log.info("browser reap failed")

    threading.Thread(target=loop, name="browser-reaper", daemon=True).start()
