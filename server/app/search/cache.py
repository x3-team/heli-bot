"""Cache of public pages Heli has read, with full-text search over it.

SQLite: an FTS5 table (web_pages_fts, rowid = web_pages.id) kept in step by store()/prune().
Other databases: word prefixes matched with LIKE, ranked in Python. Both search the same rows.

Every call here is best effort: the caller opens a short session, and a missing table (old database)
or any database error means "no cache", never a failed tool call.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.masking import mask_text
from app.models import WebPage

log = logging.getLogger(__name__)

TEXT_MAX = 20_000
DOCS_TTL = timedelta(days=30)
PAGE_TTL = timedelta(days=7)
MAX_PAGES = 50_000
SNIPPET = 300

_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)
_STOP = {
    "и", "в", "на", "по", "с", "за", "для", "не", "что", "как", "это", "или", "из", "к", "от", "до", "у", "о",
    "а", "но", "ли", "the", "a", "of", "to", "in", "and", "is", "how", "what", "где", "какой", "какие",
    "сколько", "мне", "нам", "сайт", "официальный",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stems(query: str) -> list[str]:
    """Search words cut to 5 letters, so «тарифы», «тарифов», «тарифный» meet."""
    seen: list[str] = []
    for word in _WORD.findall(query or ""):
        low = word.lower()
        if low in _STOP or len(low) < 2:
            continue
        stem = low[:5]
        if stem not in seen:
            seen.append(stem)
    return seen[:8]


def ttl_for(source: str, url: str) -> timedelta:
    from app.llm.tools import official_rank

    return DOCS_TTL if source == "docs" or official_rank(url) == 0 else PAGE_TTL


def is_fresh(page: WebPage, now: datetime | None = None) -> bool:
    return (now or _now()) - _aware(page.fetched_at) <= ttl_for(page.source, page.url)


@contextmanager
def cache_session():
    """A short session of its own; yields None when the database is not reachable."""
    try:
        from app.db import get_session_factory

        session = get_session_factory()()
    except Exception:  # noqa: BLE001
        yield None
        return
    try:
        yield session
        session.commit()
    except Exception:  # noqa: BLE001
        log.info("web cache: database error, skipped")
        session.rollback()
    finally:
        session.close()


def _is_sqlite(session: Session) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def _ensure_fts(session: Session) -> bool:
    if not _is_sqlite(session):
        return False
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if getattr(engine, "_heli_fts", False):
        return True
    try:
        session.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS web_pages_fts USING fts5("
                "title, body, tokenize='unicode61 remove_diacritics 2')"
            )
        )
    except Exception:  # noqa: BLE001  (SQLite without FTS5)
        log.info("web cache: FTS5 not available, LIKE search")
        return False
    engine._heli_fts = True  # noqa: SLF001  (checked once per engine)
    return True


def get(session: Session, url: str) -> WebPage | None:
    return session.scalar(select(WebPage).where(WebPage.url == url[:1000]))


def store(
    session: Session,
    url: str,
    title: str,
    body: str,
    *,
    source: str = "agent",
    etag: str | None = None,
) -> WebPage:
    url = url[:1000]
    title = mask_text((title or "").strip())[:500]
    body = mask_text((body or "").strip())[:TEXT_MAX]
    page = get(session, url)
    now = _now()
    if page is None:
        page = WebPage(url=url, host=(urlsplit(url).hostname or "")[:255], source=source)
        session.add(page)
    elif page.source == "docs":
        source = "docs"  # an agent read of an indexed help page keeps it in the help index
    page.title = title
    page.text = body
    page.source = source
    page.etag = etag
    page.fetched_at = now
    page.last_used_at = now
    session.flush()
    if _ensure_fts(session):
        session.execute(text("DELETE FROM web_pages_fts WHERE rowid = :id"), {"id": page.id})
        session.execute(
            text("INSERT INTO web_pages_fts (rowid, title, body) VALUES (:id, :title, :body)"),
            {"id": page.id, "title": title, "body": body},
        )
    return page


def touch(session: Session, page: WebPage) -> None:
    page.last_used_at = _now()
    session.flush()


def search(session: Session, query: str, limit: int = 5, source: str | None = None) -> list[WebPage]:
    words = stems(query)
    if not words:
        return []
    if _ensure_fts(session):
        match = " OR ".join(f'"{w}"*' for w in words)
        sql = (
            "SELECT p.id FROM web_pages_fts f JOIN web_pages p ON p.id = f.rowid "
            "WHERE web_pages_fts MATCH :match"
            + (" AND p.source = :source" if source else "")
            + " ORDER BY bm25(web_pages_fts, 5.0, 1.0) LIMIT :limit"
        )
        params = {"match": match, "limit": limit * 3}
        if source:
            params["source"] = source
        try:
            ids = [row[0] for row in session.execute(text(sql), params)]
        except Exception:  # noqa: BLE001
            log.info("web cache: FTS query failed")
            ids = []
        pages = {p.id: p for p in session.scalars(select(WebPage).where(WebPage.id.in_(ids)))} if ids else {}
        ranked = [pages[i] for i in ids if i in pages]
    else:
        cond = [or_(func.lower(WebPage.title).contains(w), func.lower(WebPage.text).contains(w)) for w in words]
        stmt = select(WebPage).where(or_(*cond))
        if source:
            stmt = stmt.where(WebPage.source == source)
        candidates = list(session.scalars(stmt.limit(500)))
        ranked = sorted(candidates, key=lambda p: _score(p, words), reverse=True)
    # Pages that match only one word of a long query are noise.
    need = 1 if len(words) <= 2 else 2
    good = [p for p in ranked if _matched(p, words) >= need]
    return good[:limit]


def _matched(page: WebPage, words: list[str]) -> int:
    hay = f"{page.title} {page.text}".lower()
    return sum(1 for w in words if w in hay)


def _score(page: WebPage, words: list[str]) -> float:
    title = page.title.lower()
    body = page.text.lower()
    return sum(3.0 * (w in title) + min(body.count(w), 5) * 0.5 for w in words)


def snippet(page: WebPage, query: str) -> str:
    body = page.text
    low = body.lower()
    for word in stems(query):
        at = low.find(word)
        if at >= 0:
            start = max(0, at - 80)
            return ("…" if start else "") + " ".join(body[start : start + SNIPPET].split()) + "…"
    return " ".join(body[:SNIPPET].split())


def prune(session: Session, max_pages: int = MAX_PAGES) -> int:
    """Drop the least recently used agent pages over the cap (help pages go last)."""
    total = int(session.scalar(select(func.count()).select_from(WebPage)) or 0)
    extra = total - max_pages
    if extra <= 0:
        return 0
    order = (WebPage.source == "docs", func.coalesce(WebPage.last_used_at, WebPage.fetched_at))
    ids = list(session.scalars(select(WebPage.id).order_by(*order).limit(extra)))
    if not ids:
        return 0
    if _ensure_fts(session):
        for chunk in range(0, len(ids), 500):
            part = ids[chunk : chunk + 500]
            session.execute(
                text(f"DELETE FROM web_pages_fts WHERE rowid IN ({','.join(str(int(i)) for i in part)})")
            )
    session.execute(delete(WebPage).where(WebPage.id.in_(ids)))
    session.flush()
    return len(ids)
