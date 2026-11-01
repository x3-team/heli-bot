"""The agent loop: ask the model, run the tools it calls, feed results back, repeat.

It knows nothing about the database or HTTP. The caller passes:
- complete(messages, tools) -> Completion   (metered model call)
- call_tool(call_id, name, args) -> ToolResult
- on_event(dict)                            (step stream + trajectory)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.agent.control import Control, NoControl, Stopped
from app.llm.client import Completion
from app.llm.tools import parse_args

TOOL_RESULT_LIMIT = 9000
AUTOPAUSE_RATIO = Decimal("1.5")
CONFIRM_TOOL = "ask_confirmation"


@dataclass
class Budget:
    max_steps: int = 15
    max_rub: Decimal | None = None
    estimate_rub: Decimal | None = None
    autopause: bool = True


@dataclass
class ToolResult:
    content: str
    error: str | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Outcome:
    final_answer: str
    steps: int
    cost_rub: Decimal
    stopped: str | None
    alias: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def approved(result: ToolResult) -> bool:
    return result.error is None and result.content.strip().lower().startswith("approved")


def run_agent(
    messages: list[dict],
    *,
    tools: list[dict],
    call_tool: Callable[[str, str, dict], ToolResult],
    complete: Callable[[list[dict], list[dict] | None], Completion],
    budget: Budget,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    start_cost: Decimal = Decimal("0"),
) -> Outcome:
    control = control or NoControl()
    emit = on_event or (lambda event: None)
    spent = start_cost
    threshold = budget.estimate_rub * AUTOPAUSE_RATIO if budget.estimate_rub else None
    out = Outcome(final_answer="", steps=0, cost_rub=spent, stopped=None)
    try:
        for round_index in range(budget.max_steps):
            control.checkpoint()
            if budget.max_rub is not None and spent >= budget.max_rub:
                out.stopped = "budget"
                break
            if budget.autopause and threshold is not None and spent > threshold:
                control.auto_pause(
                    "cost",
                    {"spent_rub": str(spent), "estimate_rub": str(budget.estimate_rub)},
                )
                # Resumed: pause again if the run costs another half of what it has spent.
                threshold = spent * AUTOPAUSE_RATIO
            last = round_index == budget.max_steps - 1
            completion = complete(messages, None if last else tools)
            out.steps += 1
            spent += completion.cost_rub
            out.alias = completion.alias
            out.input_tokens += completion.input_tokens
            out.output_tokens += completion.output_tokens
            emit(
                {
                    "type": "step",
                    "kind": "answer" if not completion.tool_calls else "model",
                    "model_alias": completion.alias,
                    "text": completion.text,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "cost_rub": completion.cost_rub,
                    "spent_rub": spent,
                }
            )
            if not completion.tool_calls:
                out.final_answer = completion.text
                break
            messages.append(completion.assistant_message())
            stop_after = None
            for call in completion.tool_calls:
                if stop_after:
                    # The model must see an answer to every call it made.
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": "Не выполнено."})
                    continue
                control.checkpoint()
                args = parse_args(call.arguments)
                emit({"type": "step", "kind": "tool_call", "call_id": call.id, "tool": call.name, "args": args})
                result = call_tool(call.id, call.name, args)
                content = result.content[:TOOL_RESULT_LIMIT]
                emit(
                    {
                        "type": "step",
                        "kind": "tool_result",
                        "call_id": call.id,
                        "tool": call.name,
                        "args": args,
                        "text": content,
                        "error": result.error,
                        "data": result.data,
                    }
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if call.name == CONFIRM_TOOL and not approved(result):
                    stop_after = "confirm"
            if stop_after:
                out.stopped = stop_after
                out.final_answer = "Остановился: действие не подтверждено."
                break
        else:
            out.stopped = "max_steps"
    except Stopped as exc:
        out.stopped = exc.reason
    out.cost_rub = spent
    return out
