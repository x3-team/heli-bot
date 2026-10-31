"""Masked incident reports from the desktop client."""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.masking import mask_text
from app.models import IncidentReport, User

log = logging.getLogger(__name__)

MAX_LOG = 32_000
MAX_SCREENSHOT = 2_000_000


class SupportError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def create_incident(
    session: Session,
    user: User,
    *,
    message: str,
    log_excerpt: str,
    screenshot_base64: str | None,
) -> IncidentReport:
    text = (message or "").strip()
    if not text:
        raise SupportError("bad_message")
    logs = mask_text((log_excerpt or "")[:MAX_LOG])
    shot = _decode_screenshot(screenshot_base64)
    row = IncidentReport(
        id=uuid.uuid4(),
        user_id=user.id,
        message=mask_text(text)[:4000],
        log_excerpt=logs,
        screenshot=shot,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    log.info("incident report stored id=%s", row.id)
    return row


def _decode_screenshot(raw: str | None) -> bytes | None:
    if not raw:
        return None
    payload = raw.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SupportError("bad_screenshot") from exc
    if len(data) > MAX_SCREENSHOT:
        raise SupportError("screenshot_too_large")
    return data
