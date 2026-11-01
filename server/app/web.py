"""Internet access for the agent: web search and reading a page.

Search goes through an official API (Yandex Search API or Tavily) when a key is set.
Without a key it falls back to Wikipedia search, which needs no key. Page reading
works without any key. Private and loopback addresses are refused, so the agent
cannot reach services on the server itself.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "HeliBot/0.1 (+https://getheli.ru)"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
PAGE_TEXT_LIMIT = 8000
LINKS_LIMIT = 25


class WebError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)


# --- search -----------------------------------------------------------------


def search_provider() -> str:
    explicit = os.environ.get("HELI_SEARCH_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("YANDEX_SEARCH_API_KEY") and os.environ.get("YANDEX_SEARCH_FOLDER_ID"):
        return "yandex"
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    return "wikipedia"


def web_search(query: str, limit: int = 6, http: httpx.Client | None = None) -> list[SearchHit]:
    query = query.strip()
    if not query:
        raise WebError("empty_query")
    limit = max(1, min(limit, 10))
    provider = search_provider()
    own = http is None
    client = http or _client()
    try:
        if provider == "yandex":
            return _yandex(client, query, limit)
        if provider == "tavily":
            return _tavily(client, query, limit)
        return _wikipedia(client, query, limit)
    except WebError:
        raise
    except Exception:
        log.info("web search failed provider=%s", provider)
        raise WebError("search_failed") from None
    finally:
        if own:
            client.close()


def _yandex(client: httpx.Client, query: str, limit: int) -> list[SearchHit]:
    response = client.post(
        "https://searchapi.api.cloud.yandex.net/v2/web/search",
        headers={"Authorization": f"Api-Key {os.environ['YANDEX_SEARCH_API_KEY']}"},
        json={
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
            "folderId": os.environ["YANDEX_SEARCH_FOLDER_ID"],
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(limit), "docsInGroup": "1"},
            "responseFormat": "FORMAT_XML",
            "l10n": "LOCALIZATION_RU",
        },
    )
    if response.status_code != 200:
        log.info("yandex search status=%s", response.status_code)
        raise WebError("search_failed")
    raw = base64.b64decode(response.json().get("rawData", ""))
    return parse_yandex_xml(raw)[:limit]


def parse_yandex_xml(raw: bytes) -> list[SearchHit]:
    root = ET.fromstring(raw)
    hits: list[SearchHit] = []
    for doc in root.iter("doc"):
        url = _flat(doc.find("url"))
        if not url:
            continue
        title = _flat(doc.find("title"))
        passages = [_flat(p) for p in doc.iter("passage")]
        snippet = " ".join(p for p in passages if p) or _flat(doc.find("headline"))
        hits.append(SearchHit(title=title or url, url=url, snippet=snippet))
    return hits


def _flat(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def _tavily(client: httpx.Client, query: str, limit: int) -> list[SearchHit]:
    response = client.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
        json={"query": query, "max_results": limit},
    )
    if response.status_code != 200:
        log.info("tavily search status=%s", response.status_code)
        raise WebError("search_failed")
    return [
        SearchHit(
            title=str(item.get("title") or item.get("url") or ""),
            url=str(item.get("url") or ""),
            snippet=str(item.get("content") or "")[:500],
        )
        for item in response.json().get("results", [])
        if item.get("url")
    ][:limit]


def _wikipedia(client: httpx.Client, query: str, limit: int) -> list[SearchHit]:
    lang = "ru" if re.search(r"[а-яё]", query, re.I) else "en"
    response = client.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": str(limit),
            "format": "json",
            "utf8": "1",
        },
    )
    if response.status_code != 200:
        raise WebError("search_failed")
    hits = []
    for item in response.json().get("query", {}).get("search", []):
        title = str(item.get("title", ""))
        snippet = re.sub(r"<[^>]+>", "", unescape(str(item.get("snippet", ""))))
        url = f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
    return hits


# --- page reading -----------------------------------------------------------


def check_url(url: str, resolver=socket.getaddrinfo) -> str:
    """Allow only public http(s) addresses. Returns the normalised URL.

    httpx resolves the name again when it connects, so a DNS answer that changes
    between the two lookups is not covered. Fine for v0.1 on a host without
    sensitive internal HTTP services; pin the address before opening it to users.
    """
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise WebError("bad_url")
    if parts.username or parts.password:
        raise WebError("bad_url")
    host = parts.hostname
    if host.lower() in {"localhost"} or host.lower().endswith((".localhost", ".local", ".internal")):
        raise WebError("blocked_address")
    try:
        infos = resolver(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        raise WebError("dns_failed") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global or address.is_multicast:
            raise WebError("blocked_address")
    return url


def fetch_page(url: str, http: httpx.Client | None = None, resolver=socket.getaddrinfo) -> Page:
    own = http is None
    client = http or _client()
    try:
        current = check_url(url, resolver)
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    current = check_url(urljoin(current, location), resolver)
                    continue
                if response.status_code >= 400:
                    raise WebError(f"http_{response.status_code}")
                kind = response.headers.get("content-type", "").lower()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                encoding = response.encoding or "utf-8"
            raw = bytes(body).decode(encoding, errors="replace")
            if "html" in kind or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                return parse_html(current, raw)
            if kind.startswith("text/") or "json" in kind or "xml" in kind:
                return Page(url=current, title="", text=_squeeze(raw)[:PAGE_TEXT_LIMIT])
            raise WebError("unsupported_content")
        raise WebError("too_many_redirects")
    except WebError:
        raise
    except httpx.TimeoutException:
        raise WebError("timeout") from None
    except Exception:
        log.info("page fetch failed")
        raise WebError("fetch_failed") from None
    finally:
        if own:
            client.close()


class _TextParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td"}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.title = ""
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip += 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._href = urljoin(self.base, href)
                self._anchor = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            label = " ".join("".join(self._anchor).split())
            if label and len(self.links) < 200:
                self.links.append((label[:80], self._href))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


def parse_html(url: str, raw: str) -> Page:
    parser = _TextParser(url)
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    seen: set[str] = set()
    links = []
    for label, href in parser.links:
        if href in seen or not href.startswith("http"):
            continue
        seen.add(href)
        links.append((label, href))
        if len(links) >= LINKS_LIMIT:
            break
    return Page(
        url=url,
        title=" ".join(parser.title.split()),
        text=_squeeze("".join(parser.parts))[:PAGE_TEXT_LIMIT],
        links=links,
    )


def _squeeze(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
    )
