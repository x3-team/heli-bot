import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app import web
from app.llm import tools as agent_tools
from app.llm.client import HeliClient
from app.llm.run import run_turn
from app.models import TaskStep, User
from app.trajectories import start_task
from app.web import WebError, check_url, fetch_page, parse_html, parse_yandex_xml, web_search


def _resolver(address: str):
    def resolve(host, port):
        return [(2, 1, 6, "", (address, port))]

    return resolve


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"],
)
def test_private_addresses_are_refused(address) -> None:
    with pytest.raises(WebError) as caught:
        check_url("http://example.com/", _resolver(address))
    assert caught.value.code == "blocked_address"


def test_bad_urls_are_refused() -> None:
    for url in ["ftp://example.com", "http://user:pw@example.com", "http://localhost:8001/v1"]:
        with pytest.raises(WebError):
            check_url(url, _resolver("93.184.216.34"))


def test_public_url_without_scheme_gets_https() -> None:
    assert check_url("amocrm.ru", _resolver("93.184.216.34")) == "https://amocrm.ru"


def test_parse_html_keeps_text_title_and_links() -> None:
    page = parse_html(
        "https://example.com/a/",
        "<html><head><title> Пример </title><script>var x=1</script></head>"
        "<body><h1>Заголовок</h1><p>Текст&nbsp;страницы</p>"
        "<a href='/price'>Цены</a><a href='#top'>наверх</a></body></html>",
    )
    assert page.title == "Пример"
    assert "Заголовок" in page.text and "Текст" in page.text
    assert "var x" not in page.text
    assert page.links == [("Цены", "https://example.com/price")]


def test_fetch_page_follows_redirect_and_rechecks_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "https://example.com/new"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>Новая</title><p>ok</p>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    page = fetch_page("https://example.com/old", http=client, resolver=_resolver("93.184.216.34"))
    assert page.url == "https://example.com/new"
    assert page.title == "Новая"


def test_fetch_page_refuses_redirect_into_private_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://internal.test/"})

    def resolve(host, port):
        return [(2, 1, 6, "", ("10.0.0.1" if host == "internal.test" else "93.184.216.34", port))]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(WebError) as caught:
        fetch_page("https://example.com/", http=client, resolver=resolve)
    assert caught.value.code == "blocked_address"


def test_yandex_xml_is_parsed() -> None:
    raw = (
        "<yandexsearch><response><results><grouping>"
        "<group><doc><url>https://www.amocrm.ru/</url><title><hlword>amoCRM</hlword> — CRM</title>"
        "<passages><passage>Система для продаж</passage></passages></doc></group>"
        "</grouping></results></response></yandexsearch>"
    ).encode()
    hits = parse_yandex_xml(raw)
    assert hits[0].url == "https://www.amocrm.ru/"
    assert hits[0].title == "amoCRM — CRM"
    assert hits[0].snippet == "Система для продаж"


def test_search_uses_yandex_when_key_is_set(monkeypatch) -> None:
    monkeypatch.delenv("HELI_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "k")
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", "f")
    xml = "<r><doc><url>https://a.ru/</url><title>A</title></doc></r>".encode()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"rawData": base64.b64encode(xml).decode()})

    hits = web_search("амо", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["host"] == "searchapi.api.cloud.yandex.net"
    assert seen["auth"] == "Api-Key k"
    assert seen["body"]["query"]["queryText"] == "амо"
    assert [hit.url for hit in hits] == ["https://a.ru/"]


def test_search_falls_back_to_wikipedia_without_keys(monkeypatch) -> None:
    for name in ["HELI_SEARCH_PROVIDER", "YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_FOLDER_ID", "TAVILY_API_KEY"]:
        monkeypatch.delenv(name, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ru.wikipedia.org"
        return httpx.Response(
            200,
            json={"query": {"search": [{"title": "Битрикс24", "snippet": "<span>CRM</span> система"}]}},
        )

    hits = web_search("битрикс", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert hits[0].url == "https://ru.wikipedia.org/wiki/Битрикс24"
    assert hits[0].snippet == "CRM система"


class _ToolScript:
    """Fake gateway: classify, then one tool call, then the answer."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        step = len(self.requests)

        class Usage:
            prompt_tokens = 10
            completion_tokens = 4

        class Function:
            name = "open_url"
            arguments = '{"url": "https://www.amocrm.ru/"}'

        class Call:
            id = "call_1"
            function = Function()

        class Message:
            content = (
                '{"task_type":"search","complexity":1}'
                if step == 1
                else ("" if step == 2 else "Официальный сайт: https://www.amocrm.ru/")
            )
            tool_calls = [Call()] if step == 2 else None

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


def test_run_turn_calls_tools_and_feeds_results_back(session, monkeypatch) -> None:
    opened = []

    def fake_fetch(url):
        opened.append(url)
        return web.Page(url=url, title="amoCRM", text="CRM для продаж")

    monkeypatch.setattr(agent_tools, "fetch_page", fake_fetch)
    user = User(
        email="web@example.com",
        consent_pdn=True,
        consent_training=True,
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.flush()
    task = start_task(session, user, "Дай ссылку на amoCRM", {})
    script = _ToolScript()
    sdk = type("SDK", (), {})()
    sdk.chat = type("Chat", (), {"completions": script})()
    turn = run_turn(session, user, task, HeliClient(sdk))

    assert opened == ["https://www.amocrm.ru/"]
    assert turn.text == "Официальный сайт: https://www.amocrm.ru/"
    assert "tools" not in script.requests[0]
    offered = {tool["function"]["name"] for tool in script.requests[1]["tools"]}
    # save_file / read_file come with the files bridge (app/files.py) when the app registered it.
    assert offered - {"save_file", "read_file"} == {"web_search", "open_url"}
    final_messages = script.requests[2]["messages"]
    assert final_messages[-2]["tool_calls"][0]["function"]["name"] == "open_url"
    assert final_messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": final_messages[-1]["content"]}
    assert "CRM для продаж" in final_messages[-1]["content"]
    steps = list(session.scalars(select(TaskStep).where(TaskStep.task_id == task.id)))
    assert [step.tool_name for step in steps if step.tool_name] == ["open_url"]
