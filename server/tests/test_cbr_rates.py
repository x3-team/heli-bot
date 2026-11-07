"""ЦБ РФ: курс валют через XML_daily.asp, а не SOAP DailyInfo.asmx (прогон 07.11, ja-web-05)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.agent import http_tool
from app.models import User

PUBLIC_DNS = lambda host, port: [(0, 0, 0, "", ("93.184.216.34", port))]  # noqa: E731
SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body><GetCursOnDateXML xmlns="http://web.cbr.ru/"><On_date>2026-10-15</On_date></GetCursOnDateXML>'
    "</soap:Body></soap:Envelope>"
)


@pytest.fixture
def user(session, monkeypatch):
    monkeypatch.delenv("HELI_HTTP_SANDBOX_HOSTS", raising=False)
    row = User(email="cbr-rates@example.com", consent_pdn=True, consent_training=False, is_admin=False,
               created_at=datetime.now(timezone.utc))
    session.add(row)
    session.flush()
    return row


def _prepare(args: dict):
    return http_tool.prepare(args, lambda name: None, resolver=PUBLIC_DNS)


@pytest.mark.parametrize("args", [
    {"method": "POST", "url": "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx", "json": SOAP_ENVELOPE,
     "headers": {"Content-Type": "text/xml; charset=utf-8"}},
    {"method": "POST", "url": "https://cbr.ru/DailyInfoWebServ/DailyInfo.asmx",
     "headers": {"SOAPAction": "http://web.cbr.ru/GetCursDynamic"}, "json": {}},
    {"method": "GET", "url": "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx/GetCursOnDateXML",
     "query": {"On_date": "2026-10-15"}},
    {"method": "POST", "url": "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx/EnumValutesXML", "form": {"Seld": "false"}},
])
def test_soap_currency_call_is_refused_with_the_xml_daily_hint(args) -> None:
    with pytest.raises(http_tool.HttpToolError) as err:
        _prepare(args)
    assert err.value.code == http_tool.CBR_SOAP_CODE
    assert "XML_daily.asp" in err.value.text and "date_req" in err.value.text


def test_soap_refusal_never_reaches_the_network(session, user) -> None:
    sent: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, text="<ok/>")

    res = http_tool.execute(session, user, None, "http_request",
                            {"method": "POST", "url": "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx",
                             "json": SOAP_ENVELOPE},
                            transport=httpx.MockTransport(transport), resolver=PUBLIC_DNS)
    assert res.error == http_tool.CBR_SOAP_CODE and sent == []
    assert "XML_daily.asp" in res.content


def test_xml_daily_get_goes_out_as_a_read() -> None:
    req, _ = _prepare({"method": "GET", "url": "https://www.cbr.ru/scripts/XML_daily.asp",
                       "query": {"date_req": "15/10/2026"}})
    assert req.mode == "read" and req.query == {"date_req": "15/10/2026"}


def test_other_dailyinfo_methods_and_other_hosts_are_left_alone() -> None:
    # Key rate lives only in DailyInfo: not a currency call, not refused (a POST there still asks as usual).
    req, _ = _prepare({"method": "GET", "url": "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx/KeyRateXML",
                       "query": {"fromDate": "2026-10-01", "ToDate": "2026-10-15"}})
    assert req.mode == "read"
    assert not http_tool.is_cbr_soap_currency("https://example.com/DailyInfo.asmx/GetCursOnDate", {}, None, None)


def test_tool_description_points_at_xml_daily() -> None:
    description = http_tool.TOOL["function"]["description"]
    assert "XML_daily.asp" in description and "date_req" in description


def test_knowledge_seed_answers_a_cbr_rate_question(session) -> None:
    from app.knowledge import relevant_notes

    notes = relevant_notes(session, "Какой был курс евро ЦБ РФ на 15 октября 2026?")
    assert notes and "XML_daily.asp?date_req=" in notes[0]
    assert "Nominal" in notes[0] and "DailyInfo.asmx" in notes[0]


def test_eval_world_note_is_the_production_seed() -> None:
    from app.knowledge import SEEDS

    topic, text, source = next(s for s in SEEDS if "XML_daily" in s[0])
    world = json.loads((Path(__file__).parents[1] / "evals" / "base_world.json").read_text())
    note = next(n for n in world["knowledge"] if n["topic"] == topic)
    assert (note["fact"], note["source_url"]) == (text, source)


def test_eval_inn_08_fails_a_soap_agent_and_a_wrong_date() -> None:
    import copy

    from evals.agents import golden_agent
    from evals.runner import load_scenarios, run_scenario

    scenario = next(s for s in load_scenarios() if s["id"] == "inn-08")
    assert run_scenario(scenario, golden_agent(scenario)).success

    soap = copy.deepcopy(scenario)
    soap["golden"]["calls"] = [{"tool": "http_request", "args": {
        "method": "POST", "url": "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx", "json": SOAP_ENVELOPE}}]
    assert not run_scenario(soap, golden_agent(soap)).success

    today = copy.deepcopy(scenario)  # no date_req: today's rate 100,15 instead of the asked day
    today["golden"]["calls"] = [{"tool": "http_request", "args": {
        "method": "GET", "url": "https://www.cbr.ru/scripts/XML_daily.asp"}}]
    today["golden"]["answer"] = "Курс евро ЦБ — 100,15 руб., 1 200 евро = 120 180 руб."
    assert not run_scenario(today, golden_agent(today)).success
