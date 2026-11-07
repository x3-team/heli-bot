"""Index of official help: read the sitemaps of the sites in OFFICIAL_SOURCES and cache their pages.

Polite by design: robots.txt is honoured for the HeliBot user agent, at most one request per
CRAWL_DELAY_S per site, a page cap per site per run, and a page already indexed and still fresh
is not read again. Runs from `python -m app.cli crawl-docs` or, with HELI_DOCS_CRAWL=1, once a day
in a background thread of the API process.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.search import cache

log = logging.getLogger(__name__)

AGENT_TOKEN = "HeliBot"
CRAWL_DELAY_S = 1.0
PER_SITE = 100
MAX_SITEMAPS = 8
MAX_SITEMAP_BYTES = 10_000_000
DAY_S = 24 * 3600
_LINK_LINE = re.compile(r"^- [^\n]*?: (https?://\S+)$", re.M)


@dataclass
class Site:
    host: str
    prefixes: list[str] = field(default_factory=list)  # paths to keep; [] = whole host

    def wants(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host.removeprefix("www.") != self.host.removeprefix("www."):
            return False
        return not self.prefixes or any(parts.path.startswith(p) for p in self.prefixes)


def official_sites() -> list[Site]:
    from app.llm.tools import OFFICIAL_SOURCES

    sites: dict[str, Site] = {}
    whole: set[str] = set()
    for source in OFFICIAL_SOURCES:
        host, _, path = source.partition("/")
        site = sites.setdefault(host, Site(host=host))
        if not path:
            whole.add(host)
        elif "/" + path not in site.prefixes:
            site.prefixes.append("/" + path)
    for host in whole:
        sites[host].prefixes = []  # a bare host means the whole site
    return list(sites.values())


@dataclass
class CrawlReport:
    site: str
    sitemap_urls: int = 0
    stored: int = 0
    skipped_fresh: int = 0
    refused_robots: int = 0
    errors: int = 0
    note: str = ""


class Crawler:
    def __init__(self, http: httpx.Client | None = None, sleep=time.sleep, fetch=None) -> None:
        from app.web import USER_AGENT, _client

        self.http = http or _client()
        self.user_agent = USER_AGENT
        self.sleep = sleep
        self._fetch = fetch
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = CRAWL_DELAY_S - (time.monotonic() - last)
            if gap > 0:
                self.sleep(gap)
        self._last[host] = time.monotonic()

    def _get(self, url: str) -> httpx.Response | None:
        from app.web import check_url

        try:
            check_url(url)
        except Exception:  # noqa: BLE001
            return None
        self._wait(urlsplit(url).hostname or "")
        try:
            response = self.http.get(url, follow_redirects=True)
        except Exception:  # noqa: BLE001
            return None
        return response

    def robots(self, site: Site) -> tuple[RobotFileParser, list[str]]:
        parser = RobotFileParser()
        response = self._get(f"https://{site.host}/robots.txt")
        if response is None or response.status_code >= 500:
            parser.disallow_all = True  # unknown: do not crawl
            return parser, []
        if response.status_code in (401, 403) or response.status_code >= 420:
            parser.disallow_all = True  # the site turns us away: do not crawl
            return parser, []
        if response.status_code >= 400:
            parser.allow_all = True  # no robots.txt: allowed
            return parser, []
        lines = response.text.splitlines()
        parser.parse(lines)
        sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("sitemap:")]
        return parser, sitemaps

    def sitemap_urls(self, site: Site, roots: list[str], limit: int) -> list[str]:
        queue = list(roots) or [f"https://{site.host}/sitemap.xml"]
        seen_maps: set[str] = set()
        pages: list[str] = []
        while queue and len(seen_maps) < MAX_SITEMAPS and len(pages) < limit * 20:
            current = queue.pop(0)
            if current in seen_maps:
                continue
            seen_maps.add(current)
            response = self._get(current)
            if response is None or response.status_code != 200:
                continue
            raw = response.content[:MAX_SITEMAP_BYTES]
            if current.endswith(".gz") or raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)[:MAX_SITEMAP_BYTES]
                except OSError:
                    continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            tag = root.tag.rsplit("}", 1)[-1]
            locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
            if tag == "sitemapindex":
                # Prefer sub-sitemaps whose address hints at the help section we want.
                locs.sort(key=lambda u: 0 if any(p.strip("/") in u for p in site.prefixes if p.strip("/")) else 1)
                queue.extend(locs)
            else:
                pages.extend(u for u in locs if site.wants(u))
        return pages

    def crawl_site(self, site: Site, limit: int = PER_SITE) -> CrawlReport:
        from app.web import WebError

        report = CrawlReport(site=site.host)
        robots, roots = self.robots(site)
        if robots.disallow_all:
            report.note = "robots.txt недоступен или запрещает"
            return report
        urls = self.sitemap_urls(site, roots, limit)
        report.sitemap_urls = len(urls)
        follow_links = not urls
        if follow_links:
            # No sitemap: start from the help section's own pages and follow its links.
            urls = [f"https://{site.host}{prefix}" for prefix in (site.prefixes or ["/"])]
        queued = set(urls)
        reads = 0
        while urls and report.stored < limit and reads < limit * 2:
            url = urls.pop(0)
            if not robots.can_fetch(AGENT_TOKEN, url):
                report.refused_robots += 1
                continue
            with cache.cache_session() as session:
                if session is None:
                    report.note = "нет базы"
                    return report
                known = cache.get(session, url)
                if known is not None and known.source == "docs" and cache.is_fresh(known):
                    report.skipped_fresh += 1
                    if follow_links:
                        for href in _LINK_LINE.findall(known.text):
                            if href not in queued and site.wants(href):
                                queued.add(href)
                                urls.append(href)
                    continue
            self._wait(site.host)
            reads += 1
            try:
                page = (self._fetch or _fetch_with_links)(url)
            except WebError:
                report.errors += 1
                continue
            except Exception:  # noqa: BLE001
                report.errors += 1
                continue
            if follow_links:
                for _, href in page.links:
                    href = href.split("#", 1)[0]
                    if href not in queued and site.wants(href):
                        queued.add(href)
                        urls.append(href)
            if not page.text:
                continue
            with cache.cache_session() as session:
                if session is not None:
                    body = page.text
                    if page.links:
                        body += "\n\nСсылки:\n" + "\n".join(f"- {label}: {href}" for label, href in page.links[:60])
                    cache.store(session, page.url, page.title, body, source="docs")
                    report.stored += 1
        return report

    def close(self) -> None:
        self.http.close()


def _fetch_with_links(url: str):
    from app.web import fetch_page

    return fetch_page(url, links_limit=200)


def crawl(sites: list[Site] | None = None, per_site: int = PER_SITE, crawler: Crawler | None = None) -> list[CrawlReport]:
    own = crawler is None
    crawler = crawler or Crawler()
    reports = []
    try:
        for site in sites or official_sites():
            try:
                reports.append(crawler.crawl_site(site, per_site))
            except Exception:  # noqa: BLE001
                log.info("docs crawl failed site=%s", site.host)
                reports.append(CrawlReport(site=site.host, note="ошибка"))
        with cache.cache_session() as session:
            if session is not None:
                cache.prune(session)
    finally:
        if own:
            crawler.close()
    return reports


def install_crawler() -> None:
    """Once a day in the background, only with HELI_DOCS_CRAWL=1."""
    from app.config import get_settings

    if os.environ.get("HELI_DOCS_CRAWL", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if os.environ.get("PYTEST_CURRENT_TEST") or get_settings().app_env == "test":
        return
    per_site = int(os.environ.get("HELI_DOCS_CRAWL_PER_SITE", PER_SITE) or PER_SITE)

    def loop() -> None:
        threading.Event().wait(120)  # let the API start first
        while True:
            try:
                reports = crawl(per_site=per_site)
                log.info("docs crawl: %s pages stored", sum(r.stored for r in reports))
            except Exception:  # noqa: BLE001
                log.info("docs crawl failed")
            threading.Event().wait(DAY_S)

    threading.Thread(target=loop, name="docs-crawler", daemon=True).start()
