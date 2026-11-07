"""СДЭК training API (api.edu.cdek.ru): the server holds the OAuth token, the model gets a stub.

The model never sees a real СДЭК token. A POST to api.edu.cdek.ru/v2/oauth/token is answered by the
server itself: it gets (or reuses) a real token with the server's own client_id/secret and hands the model
the placeholder {{secret:api.edu.cdek.ru}} as access_token. The model then writes
«Authorization: Bearer {{secret:api.edu.cdek.ru}}», and app.agent.http_tool swaps in the real token right
before a request to api.edu.cdek.ru (and nowhere else). A token echoed in a response is cut out.

Credentials: HELI_CDEK_EDU_CLIENT_ID / HELI_CDEK_EDU_CLIENT_SECRET in the server's .env. Without them the
server uses СДЭК's public test account from the API v2 documentation: it has no client's orders or money
behind it, it is the same for every integrator. Live СДЭК (api.cdek.ru) is not touched here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

HOST = "api.edu.cdek.ru"
TOKEN_PATH = "/v2/oauth/token"
PLACEHOLDER = "{{secret:api.edu.cdek.ru}}"
# СДЭК's public test account (API v2 docs, «Тестовая среда»). Not a secret: published for every integrator.
PUBLIC_CLIENT_ID = "wqGwiQx0gg8mLtiEKsUinjVSICCjtTEP"
PUBLIC_CLIENT_SECRET = "RmAmgvSgSl1yirlz9QupbzOJVqhCxcP5"
REFRESH_MARGIN_S = 60
TIMEOUT_S = 20
UNAVAILABLE_CODE = "cdek_edu_unavailable"

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}  # client_id -> (token, expires_at monotonic)


class CdekEduError(Exception):
    pass


def credentials() -> tuple[str, str]:
    cid = os.environ.get("HELI_CDEK_EDU_CLIENT_ID", "").strip()
    secret = os.environ.get("HELI_CDEK_EDU_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret
    return PUBLIC_CLIENT_ID, PUBLIC_CLIENT_SECRET


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def server_token(transport: httpx.BaseTransport | None = None) -> str:
    """A live token for api.edu.cdek.ru, cached until a minute before it expires."""
    cid, secret = credentials()
    with _lock:
        cached = _cache.get(cid)
        if cached and cached[1] - REFRESH_MARGIN_S > time.monotonic():
            return cached[0]
        try:
            with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
                response = client.post(
                    f"https://{HOST}{TOKEN_PATH}",
                    data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
                )
        except httpx.HTTPError as exc:
            log.info("cdek edu token failed: %s", type(exc).__name__)
            raise CdekEduError("unavailable") from None
        if response.status_code != 200:
            log.warning("cdek edu token refused: HTTP %s", response.status_code)
            raise CdekEduError(f"http_{response.status_code}")
        try:
            data = response.json()
            token = str(data["access_token"])
            ttl = float(data.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError):
            raise CdekEduError("bad_answer") from None
        _cache[cid] = (token, time.monotonic() + ttl)
        return token


def is_host(host: str) -> bool:
    return (host or "").lower().rstrip(".") == HOST


def is_token_call(args: dict) -> bool:
    """The model asks api.edu.cdek.ru for an OAuth token: the server answers it with a stub."""
    method = str(args.get("method") or "GET").upper().strip()
    url = str(args.get("url") or "").strip()
    parts = urlsplit(url if "://" in url else f"https://{url}")
    return method == "POST" and is_host(parts.hostname or "") and parts.path.rstrip("/") == TOKEN_PATH


def stub_answer(transport: httpx.BaseTransport | None = None) -> tuple[str, str | None]:
    """(tool result text, error code) for a token request. Checks that the server's token works first."""
    try:
        server_token(transport)
    except CdekEduError:
        return (
            "Ошибка: учебный API СДЭК сейчас не выдал токен серверу Heli. Это не данные СДЭК: не выдумывай "
            "ответ, скажи пользователю, что учебная среда СДЭК недоступна, и попробуй позже.",
            UNAVAILABLE_CODE,
        )
    body = {
        "access_token": PLACEHOLDER,
        "token_type": "bearer",
        "expires_in": 3599,
        "heli_note": (
            "Токен учебного СДЭК держит сервер Heli. В запросах к api.edu.cdek.ru ставь заголовок "
            f"Authorization: Bearer {PLACEHOLDER} — сервер подставит настоящий токен. Свои client_id и "
            "client_secret для учебной среды не нужны."
        ),
    }
    return "HTTP 200 (получение токена, данные не меняются)\n\n" + json.dumps(body, ensure_ascii=False), None


def wrap_lookup(lookup, transport: httpx.BaseTransport | None = None):
    """http_tool secret lookup where {{secret:api.edu.cdek.ru}} is the server's token, not a vault entry."""
    from app.agent.http_tool import HttpToolError
    from app.skills.hosts import host_of

    def wrapped(name: str):
        if is_host(host_of(name.strip())):
            try:
                return HOST, server_token(transport)
            except CdekEduError:
                raise HttpToolError(
                    UNAVAILABLE_CODE,
                    "Ошибка: учебный API СДЭК сейчас не выдал токен серверу Heli. Запрос не отправлен.",
                ) from None
        return lookup(name)

    return wrapped
