"""Прогон сценариев и метрики: успех, шаги, ₽, остановки."""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from evals.checks import run_check
from evals.world import TOOLS, World

SCENARIOS_DIR = Path(__file__).with_name("scenarios")
DEFAULT_BUDGET = {"max_steps": 12, "max_rub": 3.0}
STOP_REASONS = {None, "confirm", "budget", "pause", "stop", "max_steps", "error"}


class Agent(Protocol):
    """Тот же контракт, что у app.agent.evals.run."""

    def __call__(self, task: str, tools: list[dict], call_tool: Callable[[str, dict], Any],
                 budget: dict) -> dict: ...


@dataclass
class Result:
    id: str
    category: str
    title: str
    success: bool
    steps: int
    agent_steps: int
    cost_rub: float
    stopped: str | None
    seconds: float
    failures: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    tool_errors: int = 0
    answer: str = ""
    calls: list[dict] = field(default_factory=list)


def load_scenarios(directory: Path = SCENARIOS_DIR, only: str | None = None) -> list[dict]:
    scenarios: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            item.setdefault("category", path.stem)
            scenarios.append(item)
    if only:
        wanted = set(only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted or s["category"] in wanted]
    return scenarios


def _accepts(func: Callable, name: str) -> bool:
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def task_text(scenario: dict, world: World) -> str:
    return f"{scenario['task']}\n\n(Сегодня {world.state['today']}, понедельник.)"


def run_scenario(scenario: dict, agent: Agent) -> Result:
    world = World.build(scenario.get("world"), scenario.get("confirm", "approve"), scenario.get("user_answers"))
    budget = {**DEFAULT_BUDGET, **scenario.get("budget", {})}
    limit = int(budget["max_steps"])
    steps = 0

    def call_tool(name: str, args: dict | None = None) -> Any:
        nonlocal steps
        steps += 1
        if steps > limit:
            return {"error": "max_steps: лимит шагов исчерпан, заверши задачу"}
        return world.call(name, args or {})

    started = time.monotonic()
    try:
        extra = {"today": world.state["today"]} if _accepts(agent, "today") else {}
        if scenario.get("history"):
            if not _accepts(agent, "history"):
                raise TypeError("агент не принимает history, а сценарий — продолжение диалога")
            extra["history"] = scenario["history"]
        out = agent(task_text(scenario, world), TOOLS, call_tool, budget, **extra) or {}
        crash = None
    except Exception as exc:  # агент упал: это провал сценария, а не всего прогона
        out, crash = {}, f"агент упал: {type(exc).__name__}: {exc}"
    seconds = time.monotonic() - started

    answer = str(out.get("final_answer") or "")
    stopped = out.get("stopped") if not crash else "error"
    cost = float(Decimal(str(out.get("cost_rub") or 0)))
    failures = [crash] if crash else []
    for check in scenario["expect"]:
        problem = run_check(check, world, answer, stopped)
        if problem:
            failures.append(problem)
    expected_stop = scenario.get("expect_stop")
    if stopped != expected_stop and not any(c["type"] == "stopped" for c in scenario["expect"]):
        failures.append(f"остановка {stopped!r}, ждали {expected_stop!r}")
    if steps > limit:
        failures.append(f"шагов {steps} > {limit}")
    if cost > float(budget["max_rub"]):
        failures.append(f"цена {cost:.2f} ₽ > {budget['max_rub']} ₽")
    return Result(
        id=scenario["id"],
        category=scenario["category"],
        title=scenario["title"],
        success=not failures and not world.violations,
        steps=steps,
        agent_steps=int(out.get("steps") or 0),
        cost_rub=round(cost, 4),
        stopped=stopped,
        seconds=round(seconds, 2),
        failures=failures,
        violations=list(world.violations),
        tool_errors=sum(1 for c in world.calls if c.error),
        answer=answer,
        calls=[{"tool": c.tool, "args": c.args, "error": c.error} for c in world.calls],
    )


def summarize(results: list[Result]) -> dict:
    total = len(results)
    passed = sum(r.success for r in results)
    stops: dict[str, int] = {}
    for r in results:
        stops[str(r.stopped)] = stops.get(str(r.stopped), 0) + 1
    categories: dict[str, dict] = {}
    for r in results:
        cat = categories.setdefault(r.category, {"total": 0, "passed": 0})
        cat["total"] += 1
        cat["passed"] += int(r.success)
    return {
        "total": total,
        "passed": passed,
        "success_rate": round(passed / total, 4) if total else 0.0,
        "avg_steps": round(sum(r.steps for r in results) / total, 2) if total else 0.0,
        "cost_rub_total": round(sum(r.cost_rub for r in results), 4),
        "cost_rub_avg": round(sum(r.cost_rub for r in results) / total, 4) if total else 0.0,
        "stops": stops,
        "safety_violations": sum(len(r.violations) for r in results),
        "by_category": categories,
    }


def to_json(results: list[Result], meta: dict) -> dict:
    return {"meta": meta, "summary": summarize(results), "results": [asdict(r) for r in results]}


def to_markdown(results: list[Result], meta: dict) -> str:
    s = summarize(results)
    lines = [
        f"## Эталонные задачи Heli: {s['passed']}/{s['total']} ({s['success_rate']:.0%})",
        "",
        f"Агент: `{meta.get('agent')}` · шагов в среднем: {s['avg_steps']} · "
        f"₽ всего: {s['cost_rub_total']:.2f} (в среднем {s['cost_rub_avg']:.2f}) · "
        f"нарушений безопасности: {s['safety_violations']} · остановки: "
        + ", ".join(f"{k}={v}" for k, v in sorted(s["stops"].items())),
        "",
        "| Категория | Успех |",
        "|---|---|",
    ]
    for name, cat in sorted(s["by_category"].items()):
        lines.append(f"| {name} | {cat['passed']}/{cat['total']} |")
    lines += ["", "| | Сценарий | Шаги | ₽ | Остановка | Что не так |", "|---|---|---|---|---|---|"]
    for r in results:
        why = "; ".join(r.failures + r.violations).replace("|", "/")
        lines.append(f"| {'✅' if r.success else '❌'} | `{r.id}` {r.title} | {r.steps} | {r.cost_rub:.2f} | "
                     f"{r.stopped or '—'} | {why} |")
    return "\n".join(lines) + "\n"
