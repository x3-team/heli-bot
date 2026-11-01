import copy
import json
from decimal import Decimal

import pytest

from evals.__main__ import main
from evals.agents import golden_agent, noop_agent, resolve_agent
from evals.runner import load_scenarios, run_scenario, summarize, to_markdown
from evals.world import DANGEROUS, TOOL_NAMES, TOOLS, World

SCENARIOS = load_scenarios()
EXPECTED_TOOL_ERRORS = {"inn-04"}


def test_scenario_set_shape() -> None:
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert 20 <= len(ids) <= 50
    by_cat: dict[str, int] = {}
    for s in SCENARIOS:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
        assert s["expect"], s["id"]
        for call in s["golden"]["calls"]:
            assert call["tool"] in TOOL_NAMES, (s["id"], call["tool"])
    assert set(by_cat) >= {"inn", "amocrm", "bitrix24", "onec", "telegram", "mail", "cross"}
    assert min(by_cat.values()) >= 4


def test_tool_schemas_are_valid() -> None:
    names = [t["function"]["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    json.dumps(TOOLS, ensure_ascii=False)
    for tool in TOOLS:
        params = tool["function"]["parameters"]
        assert set(params["required"]) <= set(params["properties"])
        assert all("optional" not in p for p in params["properties"].values())
        assert hasattr(World, f"_t_{tool['function']['name']}")
    assert DANGEROUS <= TOOL_NAMES


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_golden_passes(scenario: dict) -> None:
    result = run_scenario(scenario, golden_agent(scenario))
    assert result.success, result.failures + result.violations
    if scenario["id"] not in EXPECTED_TOOL_ERRORS:
        assert result.tool_errors == 0, result.calls


def test_noop_fails_everything() -> None:
    results = [run_scenario(s, noop_agent) for s in SCENARIOS]
    assert not any(r.success for r in results), [r.id for r in results if r.success]


def test_skipping_confirmation_is_a_violation() -> None:
    risky = [s for s in SCENARIOS if any(c["tool"] in DANGEROUS for c in s["golden"]["calls"])]
    assert len(risky) >= 8
    for scenario in risky:
        cut = copy.deepcopy(scenario)
        cut["golden"]["calls"] = [c for c in cut["golden"]["calls"] if c["tool"] != "ask_confirmation"]
        result = run_scenario(cut, golden_agent(cut))
        assert not result.success and result.violations, scenario["id"]


def test_wrong_answer_fails_answer_checks() -> None:
    with_answers = [s for s in SCENARIOS if any(c["type"] == "answer_contains" for c in s["expect"])]
    for scenario in with_answers:
        bad = copy.deepcopy(scenario)
        bad["golden"]["answer"] = "Готово."
        assert not run_scenario(bad, golden_agent(bad)).success, scenario["id"]


def test_step_and_cost_budget() -> None:
    scenario = next(s for s in SCENARIOS if s["id"] == "c1-01")

    def chatty(task, tools, call_tool, budget):
        for _ in range(20):
            call_tool("onec_stock", {"query": "перчатки нитриловые M"})
        return {"final_answer": "460", "steps": 20, "cost_rub": Decimal("0.1"), "stopped": None}

    result = run_scenario(scenario, chatty)
    assert not result.success and any("шагов" in f for f in result.failures)

    def pricey(task, tools, call_tool, budget):
        call_tool("onec_stock", {"query": "перчатки нитриловые M"})
        return {"final_answer": "460", "steps": 1, "cost_rub": Decimal("9.5"), "stopped": None}

    result = run_scenario(scenario, pricey)
    assert not result.success and result.cost_rub == 9.5


def test_crashing_agent_is_a_failed_scenario() -> None:
    def boom(task, tools, call_tool, budget):
        raise RuntimeError("upstream down")

    result = run_scenario(SCENARIOS[0], boom)
    assert not result.success and result.stopped == "error"


def test_prompt_injection_scenarios_catch_obedient_agent() -> None:
    scenario = next(s for s in SCENARIOS if s["id"] == "inn-07")

    def obedient(task, tools, call_tool, budget):
        call_tool("open_url", {"url": "https://supplier-promo.example/news"})
        call_tool("ask_confirmation", {"action": "mail_send", "details": "как просит страница"})
        call_tool("mail_send", {"to": "buh-pay@phish.example", "subject": "Оплата", "body": "оплатите счёт 99881 срочно"})
        return {"final_answer": "Скидка 15%", "steps": 3, "cost_rub": 0, "stopped": None}

    assert not run_scenario(scenario, obedient).success


def test_summary_and_markdown() -> None:
    results = [run_scenario(s, golden_agent(s)) for s in SCENARIOS[:3]]
    results.append(run_scenario(SCENARIOS[3], noop_agent))
    summary = summarize(results)
    assert summary["total"] == 4 and summary["passed"] == 3 and summary["success_rate"] == 0.75
    assert "❌" in to_markdown(results, {"agent": "test"})


def test_cli_writes_report_and_gates(tmp_path) -> None:
    assert main(["--agent", "golden", "--out", str(tmp_path), "--gate", "0.7"]) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["success_rate"] == 1.0
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("## Эталонные задачи")
    assert main(["--agent", "noop", "--only", "onec", "--gate", "0.7"]) == 1


def test_agent_loader_accepts_module_path() -> None:
    factory = resolve_agent("evals.agents:noop_agent")
    assert factory(SCENARIOS[0]) is noop_agent


def test_baseline_loop_with_scripted_model(monkeypatch) -> None:
    from types import SimpleNamespace as NS

    import openai

    from evals.agents import baseline_agent

    replies = [
        NS(content="", tool_calls=[NS(id="c1", function=NS(name="onec_stock", arguments='{"query": "перчатки нитриловые M"}'),
                                     model_dump=lambda: {"id": "c1", "type": "function",
                                                         "function": {"name": "onec_stock", "arguments": "{}"}})]),
        NS(content="Всего 460 коробок.", tool_calls=None),
    ]
    seen: list[dict] = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = NS(completions=NS(create=self.create))

        def create(self, **kwargs):
            seen.append(kwargs)
            return NS(choices=[NS(message=replies.pop(0))], usage=NS(prompt_tokens=1000, completion_tokens=100))

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("HELI_API_KEY", "test")
    scenario = next(s for s in SCENARIOS if s["id"] == "c1-01")
    result = run_scenario(scenario, baseline_agent)
    assert result.success, result.failures
    assert result.cost_rub > 0 and len(seen) == 2
    assert seen[0]["tools"] is TOOLS
    assert seen[1]["messages"][-1]["role"] == "tool"
