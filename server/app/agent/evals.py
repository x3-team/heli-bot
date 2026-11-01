"""Entry point for the eval suite: the production loop and prompt over mocked tools.

run(task, tools, call_tool, budget) -> {final_answer, steps, cost_rub, stopped}
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from app.agent.loop import Budget, ToolResult, run_agent
from app.agent.task_run import ANSWER_MAX_TOKENS, system_prompt
from app.llm.client import HeliClient


def run(
    task: str,
    tools: list[dict],
    call_tool: Callable[[str, dict], dict | str],
    budget: dict | None = None,
    *,
    client: HeliClient | None = None,
    alias: str = "heli-fast",
    estimate_rub: Decimal | None = None,
    today: str | None = None,
) -> dict:
    """`today` (ДД.ММ.ГГГГ or ISO) is the mock world's date; the prompt uses it instead of the real one."""
    budget = budget or {}
    client = client or HeliClient.from_env()

    def complete(messages, round_tools):
        return client.complete(alias, messages, max_tokens=ANSWER_MAX_TOKENS, tools=round_tools)

    def call(call_id: str, name: str, args: dict) -> ToolResult:
        raw = call_tool(name, args)
        if isinstance(raw, str):
            return ToolResult(raw)
        if isinstance(raw, dict) and name == "ask_confirmation" and "approved" in raw:
            if raw.get("approved"):
                return ToolResult("approved")
            return ToolResult(f"rejected: {raw.get('comment') or 'пользователь отказал'}")
        if isinstance(raw, dict):
            # Pass the whole result to the model: dropping fields (status-only, etc.) hid the data.
            error = raw.get("error")
            content = raw.get("content")
            text = content if isinstance(content, str) and len(raw) <= 2 else _json(raw)
            return ToolResult(text, str(error) if error else None)
        return ToolResult(_json(raw))

    max_rub = budget.get("max_rub")
    outcome = run_agent(
        [{"role": "system", "content": system_prompt(tools, today=today)}, {"role": "user", "content": task}],
        tools=tools,
        call_tool=call,
        complete=complete,
        budget=Budget(
            # The eval counts tool calls; leave one extra model round for the final answer.
            max_steps=int(budget.get("max_steps", 15)) + 1,
            max_tool_calls=int(budget.get("max_steps", 15)),
            max_rub=Decimal(str(max_rub)) if max_rub is not None else None,
            estimate_rub=estimate_rub,
        ),
    )
    return {
        "final_answer": outcome.final_answer,
        "steps": outcome.steps,
        "cost_rub": outcome.cost_rub,
        "stopped": outcome.stopped,
    }


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
