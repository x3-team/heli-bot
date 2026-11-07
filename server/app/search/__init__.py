"""Hybrid web search without paid keys.

Sources, asked in parallel, each with its own timeout (one failing never fails the search):
- the cache of pages Heli has read and the index of official help (app.search.cache);
- Wikidata: the official website of a company or service (P856);
- Wikipedia: background on a topic;
- Yandex Search API — only with HELI_SEARCH_YANDEX=1 and its key;
- Tavily — only with HELI_SEARCH_TAVILY=1 and its key;
- SearXNG — only when HELI_SEARXNG_URL points at an instance the owner runs.

Nothing here reads a search engine's result pages: their robots.txt forbids that (see README).
When the result is thin, `is_weak` tells the agent to open the official site itself and search there.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from html import unescape
from urllib.parse import urlsplit

import httpx

from app.search import cache

log = logging.getLogger(__name__)

PROVIDER_TIMEOUT_S = 8.0
# Wikimedia asks API users to keep the load low: identical lookups are answered from memory for an hour.
_MEMO_TTL_S = 3600.0
_MEMO_MAX = 500
_memo: dict[tuple[str, str], tuple[float, list]] = {}
_memo_lock = threading.Lock()


def _memoized(kind: str, query: str, job):
    key = (kind, query.strip().lower())
    now = time.monotonic()
    with _memo_lock:
        hit = _memo.get(key)
        if hit and now - hit[0] < _MEMO_TTL_S:
            return list(hit[1])
    value = list(job() or [])
    with _memo_lock:
        if len(_memo) >= _MEMO_MAX:
            _memo.pop(next(iter(_memo)))
        _memo[key] = (now, value)
    return value
_ON = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ON


def yandex_enabled() -> bool:
    if not _flag("HELI_SEARCH_YANDEX"):
        return False
    if os.environ.get("YANDEX_SEARCH_API_KEY") and os.environ.get("YANDEX_SEARCH_FOLDER_ID"):
        return True
    log.warning("HELI_SEARCH_YANDEX=1, but YANDEX_SEARCH_API_KEY / YANDEX_SEARCH_FOLDER_ID are not set")
    return False


def tavily_enabled() -> bool:
    return _flag("HELI_SEARCH_TAVILY") and bool(os.environ.get("TAVILY_API_KEY"))


def searxng_url() -> str:
    return os.environ.get("HELI_SEARXNG_URL", "").strip().rstrip("/")


def browser_serp_enabled() -> bool:
    """Search result pages of ya.ru / go.mail.ru in the cloud browser. Off by default: both sites'
    robots.txt forbid automated searches. Kept behind a flag only for the owner's own decision."""
    return _flag("HELI_SEARCH_BROWSER_SERP")


def web_api_enabled() -> bool:
    """A real web search API is on (general web results, not only our own sources)."""
    return yandex_enabled() or tavily_enabled() or bool(searxng_url())


# --- providers ----------------------------------------------------------------


def _cache_hits(query: str, limit: int):
    from app.web import SearchHit

    with cache.cache_session() as session:
        if session is None:
            return []
        pages = cache.search(session, query, limit=limit)
        hits = []
        for page in pages:
            mark = "справка, проиндексировано" if page.source == "docs" else "прочитано Heli"
            hits.append(
                SearchHit(
                    title=page.title or page.url,
                    url=page.url,
                    snippet=f"{cache.snippet(page, query)} ({mark} {page.fetched_at:%d.%m.%Y})",
                )
            )
        return hits


def _lang(query: str) -> str:
    return "ru" if re.search(r"[а-яё]", query, re.I) else "en"


def wikipedia(client: httpx.Client, query: str, limit: int):
    from app.web import SearchHit, WebError

    lang = _lang(query)
    response = client.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query, "srlimit": str(limit),
                "format": "json", "utf8": "1"},
    )
    if response.status_code != 200:
        raise WebError("search_failed")
    hits = []
    for item in response.json().get("query", {}).get("search", []):
        title = str(item.get("title", ""))
        snippet = re.sub(r"<[^>]+>", "", unescape(str(item.get("snippet", ""))))
        hits.append(SearchHit(title=title, url=f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
                              snippet=snippet))
    return hits


def wikidata_sites(client: httpx.Client, query: str, limit: int = 3):
    """Official websites (P856) of the entities that match the query."""
    from app.web import SearchHit

    lang = _lang(query)
    found = client.get(
        "https://www.wikidata.org/w/api.php",
        params={"action": "wbsearchentities", "search": _entity_words(query), "language": lang,
                "uselang": lang, "type": "item", "limit": str(limit), "format": "json"},
    )
    if found.status_code != 200:
        return []
    items = found.json().get("search", [])
    ids = [str(item.get("id")) for item in items if item.get("id")]
    if not ids:
        return []
    info = client.get(
        "https://www.wikidata.org/w/api.php",
        params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels|descriptions",
                "languages": f"{lang}|en", "format": "json"},
    )
    if info.status_code != 200:
        return []
    entities = info.json().get("entities", {})
    hits = []
    for entity_id in ids:
        entity = entities.get(entity_id) or {}
        for claim in (entity.get("claims") or {}).get("P856", [])[:1]:
            url = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            label = _pick(entity.get("labels"), lang) or entity_id
            about = _pick(entity.get("descriptions"), lang)
            hits.append(SearchHit(title=f"{label} — официальный сайт", url=url,
                                  snippet=f"{about} (Викиданные)".strip()))
    return hits


def _pick(values: dict | None, lang: str) -> str:
    values = values or {}
    for key in (lang, "ru", "en"):
        if key in values:
            return str(values[key].get("value", ""))
    return ""


_NOISE = re.compile(
    r"\b(официальн\w*|сайт\w*|ссылк\w*|цен\w*|тариф\w*|стоимост\w*|сколько|стоит|как|где|найди|дай|"
    r"price\w*|pricing|official|site|website)\b",
    re.I,
)


def _entity_words(query: str) -> str:
    """«официальный сайт amoCRM цены» → «amoCRM»: Wikidata looks names up, not questions."""
    short = " ".join(_NOISE.sub(" ", query).split())
    return short or query


def searxng(client: httpx.Client, query: str, limit: int):
    from app.web import SearchHit

    response = client.get(f"{searxng_url()}/search", params={"q": query, "format": "json", "language": "ru"})
    if response.status_code != 200:
        return []
    return [
        SearchHit(title=str(item.get("title") or item.get("url")), url=str(item["url"]),
                  snippet=str(item.get("content") or "")[:400])
        for item in response.json().get("results", [])[:limit]
        if item.get("url")
    ]


# --- the hybrid -----------------------------------------------------------------


def hybrid_search(query: str, limit: int = 8, http: httpx.Client | None = None):
    from app.web import WebError, _client, _tavily, _yandex

    own = http is None
    client = http or _client()
    jobs = {
        "cache": lambda: _cache_hits(query, 4),
        "wikidata": lambda: _memoized("wikidata", query, lambda: wikidata_sites(client, query)),
        "wikipedia": lambda: _memoized("wikipedia", query, lambda: wikipedia(client, query, 3)),
    }
    if yandex_enabled():
        jobs["yandex"] = lambda: _yandex(client, query, limit)
    if tavily_enabled():
        jobs["tavily"] = lambda: _tavily(client, query, limit)
    if searxng_url():
        jobs["searxng"] = lambda: searxng(client, query, limit)
    results: dict[str, list] = {}
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="heli-search")
    try:
        futures = {pool.submit(job): name for name, job in jobs.items()}
        done, _ = wait(futures, timeout=PROVIDER_TIMEOUT_S * 1.5)
        for future in done:
            name = futures[future]
            try:
                results[name] = list(future.result() or [])
            except Exception as exc:  # noqa: BLE001
                log.info("search provider %s failed: %s", name, type(exc).__name__)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if own:
            client.close()
    order = ["wikidata", "cache", "yandex", "tavily", "searxng", "wikipedia"]
    merged, seen = [], set()
    for name in order:
        for hit in results.get(name, []):
            key = _norm(hit.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    if not merged and not results:
        raise WebError("search_failed")
    return merged[:limit]


def _norm(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host + parts.path.rstrip("/")


def is_weak(hits) -> bool:
    """Fewer than 3 results beyond Wikipedia articles: the agent should open the site itself."""
    if web_api_enabled():
        return len(hits) < 2
    useful = [h for h in hits if "wikipedia.org" not in (urlsplit(h.url).hostname or "")]
    return len(useful) < 3
