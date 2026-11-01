"""Thread-safe pause/stop switch and the mailbox for results of client-side tools."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal

PAUSE_TIMEOUT_S = 30 * 60
CLIENT_TIMEOUT_S = 10 * 60


class Stopped(Exception):
    """The run must end now. `reason` becomes Outcome.stopped."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ClientResult:
    content: str
    error: str | None = None
    screenshot: bytes | None = None


class Control:
    """Owned by one run. The API thread flips it; the loop thread checks it between steps."""

    def __init__(self, *, pause_timeout: float = PAUSE_TIMEOUT_S, client_timeout: float = CLIENT_TIMEOUT_S) -> None:
        self._cond = threading.Condition()
        self._paused = False
        self._stopped = False
        self._results: dict[str, ClientResult] = {}
        self._waiting: set[str] = set()
        self.pause_timeout = pause_timeout
        self.client_timeout = client_timeout
        self.on_pause = None  # callable(reason, details) set by the run, emits events
        self.on_resume = None

    # API side -----------------------------------------------------------
    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._paused = False
            self._cond.notify_all()

    def deliver(self, call_id: str, result: ClientResult) -> bool:
        with self._cond:
            if call_id not in self._waiting:
                return False
            self._results[call_id] = result
            self._cond.notify_all()
            return True

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._stopped

    # Loop side ----------------------------------------------------------
    def checkpoint(self) -> None:
        """Raise Stopped if stop was pressed; block while paused."""
        with self._cond:
            if self._stopped:
                raise Stopped("stop")
            if not self._paused:
                return
        self._wait_paused("user", {})

    def auto_pause(self, reason: str, details: dict) -> None:
        with self._cond:
            if self._stopped:
                raise Stopped("stop")
            self._paused = True
        self._wait_paused(reason, details)

    def _wait_paused(self, reason: str, details: dict) -> None:
        if self.on_pause:
            self.on_pause(reason, details)
        deadline = time.monotonic() + self.pause_timeout
        with self._cond:
            while self._paused and not self._stopped:
                left = deadline - time.monotonic()
                if left <= 0:
                    self._stopped = True
                    raise Stopped("pause")
                self._cond.wait(left)
            if self._stopped:
                raise Stopped("stop")
        if self.on_resume:
            self.on_resume()

    def wait_result(self, call_id: str) -> ClientResult:
        """Block until the client posts the result of `call_id`, stop is pressed, or time runs out."""
        deadline = time.monotonic() + self.client_timeout
        with self._cond:
            self._waiting.add(call_id)
            try:
                while call_id not in self._results:
                    if self._stopped:
                        raise Stopped("stop")
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise Stopped("client_timeout")
                    self._cond.wait(left)
                return self._results.pop(call_id)
            finally:
                self._waiting.discard(call_id)


class NoControl(Control):
    """For evals and the plain JSON endpoint: nobody can resume, so auto-pause ends the run."""

    def auto_pause(self, reason: str, details: dict) -> None:
        raise Stopped("pause")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))
