"""Facts the user can see and delete. Search uses heli-embed when a key is configured."""

from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.prices import cost_rub
from app.masking import redact_secrets_text
from app.models import MemoryFact, User
from app.quota import record_charge, snapshot

log = logging.getLogger(__name__)


class MemoryError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def remember(session: Session, user: User, text: str, embedder=None) -> MemoryFact:
    cleaned = redact_secrets_text(text).strip()
    if not cleaned:
        raise MemoryError("bad_memory")
    if embedder is None and snapshot(session, user).blocked:
        vector = None
    else:
        vector = embedder(cleaned) if embedder is not None else embed_or_skip(cleaned)
    if vector is not None:
        tokens = max(1, len(cleaned) // 4)
        record_charge(session, user, cost_rub("heli-embed", tokens, 0), alias="heli-embed")
    row = MemoryFact(
        user_id=user.id,
        text=cleaned,
        embedding=vector,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def list_memory(session: Session, user: User) -> list[dict]:
    rows = session.scalars(
        select(MemoryFact).where(MemoryFact.user_id == user.id).order_by(MemoryFact.created_at)
    )
    return [_public(row) for row in rows]


def delete_memory(session: Session, user: User, fact_id: uuid.UUID) -> None:
    row = session.get(MemoryFact, fact_id)
    if row is None or row.user_id != user.id:
        raise MemoryError("not_found")
    session.delete(row)
    session.flush()


def relevant(session: Session, user: User, query: str, embedder=None, limit: int = 5) -> list[str]:
    rows = list(session.scalars(select(MemoryFact).where(MemoryFact.user_id == user.id)))
    if not rows:
        return []
    query_vec = embedder(query) if embedder is not None else embed_or_skip(query)
    ranked = sorted(rows, key=lambda row: _rank(query, query_vec, row), reverse=True)
    return [row.text for row in ranked[:limit] if _rank(query, query_vec, row) > 0]


def with_memory(text: str, facts: list[str]) -> str:
    if not facts:
        return text
    lines = "\n".join(f"- {fact}" for fact in facts)
    return f"{text}\n\nПамять:\n{lines}"


def embed_or_skip(text: str) -> list[float] | None:
    if get_settings().app_env == "test" or not os.environ.get("HELI_API_KEY", "").strip():
        return None
    try:
        from app.llm.client import HeliClient, HeliError

        return HeliClient.from_env().embed(text)
    except HeliError:
        log.info("embedding skipped")
        return None
    except Exception:
        log.info("embedding skipped")
        return None


def _public(row: MemoryFact) -> dict:
    return {"id": str(row.id), "text": row.text}


def _rank(query: str, query_vec: list[float] | None, row: MemoryFact) -> float:
    if query_vec and isinstance(row.embedding, list) and row.embedding:
        return _cosine(query_vec, [float(value) for value in row.embedding])
    return _overlap(query, row.text)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _overlap(query: str, text: str) -> float:
    words = set(query.lower().split())
    if not words:
        return 0.0
    known = set(text.lower().split())
    return len(words & known) / len(words)
