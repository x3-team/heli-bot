import base64
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import search
from app.llm import tools as agent_tools
from app.search import cache, crawl
from app.web import Page, web_search

_KEYS = ["HELI_SEARCH_YANDEX", "HELI_SEARCH_TAVILY", "HELI_SEARXNG_URL", "HELI_SEARCH_BROWSER_SERP",
         "YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_FOLDER_ID", "TAVILY_API_KEY"]


@pytest.fixture()
def db(session, monkeypatch):
    """The page cache runs on the test session instead of opening its own."""

    @contextmanager
    def same_session():
        yield session
        session.flush()

    monkeypatch.setattr(cache, "cache_session", same_session)
    search._memo.clear()
    for name in _KEYS:
        monkeypatch.delenv(name, raising=False)
    return session


def _wiki_handler(seen: list[str], yandex_xml: bytes | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        params = dict(request.url.params)
        if request.url.host == "www.wikidata.org" and params.get("action") == "wbsearchentities":
            assert params["search"] == "amoCRM"
            return httpx.Response(200, json={"search": [{"id": "Q1"}]})
        if request.url.host == "www.wikidata.org":
            return httpx.Response(200, json={"entities": {"Q1": {
                "labels": {"ru": {"value": "amoCRM"}},
                "descriptions": {"ru": {"value": "CRM-система"}},
                "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.amocrm.ru/"}}}]},
            }}})
        if request.url.host.endswith("wikipedia.org"):
            return httpx.Response(200, json={"query": {"search": [{"title": "AmoCRM", "snippet": "<b>CRM</b>"}]}})
        if request.url.host == "searchapi.api.cloud.yandex.net":
            return httpx.Response(200, json={"rawData": base64.b64encode(yandex_xml or b"<r/>").decode()})
        return httpx.Response(404)

    return handler


def test_hybrid_merges_wikidata_cache_and_wikipedia(db) -> None:
    cache.store(db, "https://www.amocrm.ru/support/tarify", "Тарифы amoCRM", "Тариф «Базовый» стоит 599 ₽", source="docs")
    seen: list[str] = []
    hits = web_search("официальный сайт amoCRM тарифы", http=httpx.Client(transport=httpx.MockTransport(_wiki_handler(seen))))
    urls = [h.url for h in hits]
    assert urls[0] == "https://www.amocrm.ru/"  # the official site from Wikidata goes first
    assert "https://www.amocrm.ru/support/tarify" in urls
    assert any("wikipedia.org" in u for u in urls)
    assert "searchapi.api.cloud.yandex.net" not in seen


def test_yandex_runs_only_behind_its_flag(db, monkeypatch) -> None:
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "k")
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", "f")
    xml = b"<r><doc><url>https://a.ru/</url><title>A</title></doc></r>"
    seen: list[str] = []
    web_search("amoCRM", http=httpx.Client(transport=httpx.MockTransport(_wiki_handler(seen, xml))))
    assert "searchapi.api.cloud.yandex.net" not in seen  # key alone is not enough

    monkeypatch.setenv("HELI_SEARCH_YANDEX", "1")
    seen.clear()
    hits = web_search("amoCRM", http=httpx.Client(transport=httpx.MockTransport(_wiki_handler(seen, xml))))
    assert "searchapi.api.cloud.yandex.net" in seen
    assert "https://a.ru/" in [h.url for h in hits]


def test_one_provider_failing_does_not_fail_the_search(db) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wikidata.org":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"query": {"search": [{"title": "Битрикс24", "snippet": "CRM"}]}})

    hits = web_search("битрикс", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert hits[0].url == "https://ru.wikipedia.org/wiki/Битрикс24"


def test_cache_finds_russian_word_forms(db) -> None:
    cache.store(db, "https://dev.moysklad.ru/doc/api/", "API МойСклад", "Остатки товаров по складам и ценам", source="docs")
    cache.store(db, "https://example.com/other", "Прочее", "Погода на завтра")
    found = cache.search(db, "остаток товара на складе")
    assert [p.url for p in found] == ["https://dev.moysklad.ru/doc/api/"]


def test_cache_text_is_masked(db) -> None:
    page = cache.store(db, "https://example.com/c", "Контакты", "Пишите на ivan.petrov@example.com")
    assert "ivan.petrov@example.com" not in page.text


def test_prune_drops_least_used_agent_pages_first(db) -> None:
    cache.store(db, "https://docs.example.com/a", "Справка", "текст справки", source="docs")
    for i in range(3):
        cache.store(db, f"https://example.com/{i}", f"Страница {i}", "текст страницы")
    removed = cache.prune(db, max_pages=2)
    assert removed == 2
    left = {p.url for p in db.query(cache.WebPage)}
    assert "https://docs.example.com/a" in left and len(left) == 2
    assert cache.search(db, "текст страницы")  # the FTS index follows the table


def test_open_url_reads_through_the_cache(db, monkeypatch) -> None:
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return Page(url=url, title="Цены", text="599 ₽ в месяц", links=[("Тарифы", "https://www.amocrm.ru/buy/")])

    monkeypatch.setattr(agent_tools, "fetch_page", fake_fetch)
    first, _ = agent_tools.call_tool("open_url", {"url": "amocrm.ru/prices"})
    second, _ = agent_tools.call_tool("open_url", {"url": "https://amocrm.ru/prices"})
    assert calls == ["https://amocrm.ru/prices"]
    assert "599 ₽" in first and "кеша" not in first
    assert "копия из кеша" in second and "amocrm.ru/buy/" in second
    agent_tools.call_tool("open_url", {"url": "https://amocrm.ru/prices", "fresh": True})
    assert len(calls) == 2


def test_stale_page_is_read_again(db, monkeypatch) -> None:
    page = cache.store(db, "https://example.com/p", "Старое", "старый текст")
    page.fetched_at = datetime.now(timezone.utc) - timedelta(days=8)
    db.flush()
    monkeypatch.setattr(agent_tools, "fetch_page", lambda url: Page(url=url, title="Новое", text="новый текст"))
    text, _ = agent_tools.call_tool("open_url", {"url": "https://example.com/p"})
    assert "новый текст" in text


def test_pages_with_secrets_in_the_address_are_not_cached(db, monkeypatch) -> None:
    monkeypatch.setattr(agent_tools, "fetch_page", lambda url: Page(url=url, title="Отчёт", text="данные"))
    agent_tools.call_tool("open_url", {"url": "https://example.com/r?token=abc"})
    assert cache.get(db, "https://example.com/r?token=abc") is None


def test_weak_result_tells_the_agent_to_open_the_site(db, monkeypatch) -> None:
    from app.web import SearchHit

    monkeypatch.setattr(agent_tools, "web_search", lambda q: [SearchHit("Вики", "https://ru.wikipedia.org/wiki/X", "")])
    text, _ = agent_tools.call_tool("web_search", {"query": "цены сервиса X"})
    assert "Открой официальный сайт сам" in text

    many = [SearchHit(f"S{i}", f"https://s{i}.ru/", "") for i in range(4)]
    monkeypatch.setattr(agent_tools, "web_search", lambda q: many)
    text, _ = agent_tools.call_tool("web_search", {"query": "цены сервиса X"})
    assert "Открой официальный сайт сам" not in text


def test_browser_serp_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HELI_SEARCH_BROWSER_SERP", raising=False)
    assert search.browser_serp_enabled() is False
    monkeypatch.setenv("HELI_SEARCH_BROWSER_SERP", "1")
    assert search.browser_serp_enabled() is True


# --- the help index crawler ---------------------------------------------------------


SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://help.example.com/docs/a</loc></url>
<url><loc>https://help.example.com/docs/private</loc></url>
<url><loc>https://help.example.com/blog/x</loc></url>
</urlset>"""


def test_crawler_follows_sitemap_and_robots(db, monkeypatch) -> None:
    monkeypatch.setattr("app.web.check_url", lambda url, resolver=None: url)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /docs/private\nSitemap: https://help.example.com/sm.xml\n")
        if request.url.path == "/sm.xml":
            return httpx.Response(200, text=SITEMAP)
        return httpx.Response(404)

    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return Page(url=url, title="Справка", text="Как настроить обмен")

    crawler = crawl.Crawler(http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, fetch=fake_fetch)
    site = crawl.Site(host="help.example.com", prefixes=["/docs"])
    report = crawler.crawl_site(site)
    assert fetched == ["https://help.example.com/docs/a"]  # blog is out of scope, private is refused by robots
    assert report.stored == 1 and report.refused_robots == 1
    assert cache.get(db, "https://help.example.com/docs/a").source == "docs"

    again = crawler.crawl_site(site)  # fresh pages are not read twice
    assert again.skipped_fresh == 1 and fetched == ["https://help.example.com/docs/a"]


def test_crawler_stops_when_robots_is_unreachable(db, monkeypatch) -> None:
    monkeypatch.setattr("app.web.check_url", lambda url, resolver=None: url)
    crawler = crawl.Crawler(
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))), sleep=lambda s: None,
        fetch=lambda url: pytest.fail("must not fetch"),
    )
    report = crawler.crawl_site(crawl.Site(host="help.example.com"))
    assert report.stored == 0 and report.note


def test_official_sites_group_prefixes_by_host() -> None:
    sites = {s.host: s for s in crawl.official_sites()}
    assert sites["yandex.ru"].prefixes == ["/support", "/dev"]
    assert sites["dev.moysklad.ru"].prefixes == []


def test_crawler_follows_links_when_there_is_no_sitemap(db, monkeypatch) -> None:
    monkeypatch.setattr("app.web.check_url", lambda url, resolver=None: url)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /bitrix/\n")
        return httpx.Response(404)

    site_pages = {
        "https://dev.example.ru/doc": [("API", "https://dev.example.ru/doc/api"), ("Чужой", "https://other.ru/x")],
        "https://dev.example.ru/doc/api": [("Назад", "https://dev.example.ru/doc")],
    }
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return Page(url=url, title="Док", text="Документация API", links=site_pages.get(url, []))

    crawler = crawl.Crawler(http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, fetch=fake_fetch)
    report = crawler.crawl_site(crawl.Site(host="dev.example.ru", prefixes=["/doc"]))
    assert fetched == ["https://dev.example.ru/doc", "https://dev.example.ru/doc/api"]
    assert report.stored == 2
