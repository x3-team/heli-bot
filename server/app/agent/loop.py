"""The agent loop: ask the model, run the tools it calls, feed results back, repeat.

It knows nothing about the database or HTTP. The caller passes:
- complete(messages, tools) -> Completion   (metered model call)
- call_tool(call_id, name, args) -> ToolResult
- on_event(dict)                            (step stream + trajectory)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.agent.control import Control, NoControl, Stopped
from app.agent.guard import Guard, describe
from app.llm.client import Completion
from app.llm.tools import parse_args

TOOL_RESULT_LIMIT = 9000
AUTOPAUSE_RATIO = Decimal("1.5")
CONFIRM_TOOL = "ask_confirmation"


@dataclass
class Budget:
    max_steps: int = 15
    # Tool calls allowed in the run (None = only max_steps model rounds count).
    max_tool_calls: int | None = None
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
    text = result.content.strip().lower()
    return result.error is None and (text.startswith("approved") or text.startswith('{"approved": true'))


FINISH_NOTE = (
    "Лимит действий исчерпан. Больше инструменты не вызывай: ответь пользователю тем, что уже нашёл, "
    "с конкретными цифрами и источниками, и скажи, что не успел."
)


def repeat_key(name: str, args: dict) -> str | None:
    """Identity of a call for the repeat check: the tool and its text arguments (limits and counts aside).
    Browser tools and confirmations are left out: the page or the user's answer can change between calls."""
    if name.startswith("browser_") or name == CONFIRM_TOOL:
        return None
    texts = {k: v.strip().lower() for k, v in args.items() if isinstance(v, str)}
    return name + json.dumps(texts, ensure_ascii=False, sort_keys=True)


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
    guard = Guard(schemas={t["function"]["name"]: t for t in tools})
    can_ask = CONFIRM_TOOL in guard.schemas
    calls_used = 0
    auto_ids = 0
    failed: dict[str, str] = {}  # same call that already failed -> its error

    def run_call(call_id: str, name: str, args: dict) -> ToolResult:
        emit({"type": "step", "kind": "tool_call", "call_id": call_id, "tool": name, "args": args})
        result = call_tool(call_id, name, args)
        content = result.content[:TOOL_RESULT_LIMIT]
        emit(
            {
                "type": "step",
                "kind": "tool_result",
                "call_id": call_id,
                "tool": name,
                "args": args,
                "text": content,
                "error": result.error,
                "data": result.data,
            }
        )
        return ToolResult(content, result.error, result.data)

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
            out_of_calls = budget.max_tool_calls is not None and calls_used >= budget.max_tool_calls
            last = round_index == budget.max_steps - 1 or out_of_calls
            round_messages = messages
            if out_of_calls:
                round_messages = messages + [{"role": "user", "content": FINISH_NOTE}]
            completion = complete(round_messages, None if last else tools)
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
            if not completion.tool_calls or last:
                out.final_answer = completion.text
                break
            messages.append(completion.assistant_message())
            stop_after = None
            for call in completion.tool_calls:
                skip = stop_after or (
                    budget.max_tool_calls is not None and calls_used >= budget.max_tool_calls
                )
                if skip:
                    # The model must see an answer to every call it made.
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": "Не выполнено."})
                    continue
                control.checkpoint()
                args = parse_args(call.arguments)
                key = repeat_key(call.name, args)
                if key and key in failed:
                    # The model repeats a call that already failed the same way: don't spend a step on it.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"Не выполнено: этот вызов уже вернул ошибку ({failed[key]}). "
                            "Не повторяй его, попробуй другие аргументы или другой инструмент.",
                        }
                    )
                    continue
                if guard.needs_approval(call.name, args) and not guard.consume(call.name):
                    # The model skipped ask_confirmation: the loop asks, and never runs the tool unasked.
                    if not can_ask:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": "Ошибка: это действие нельзя выполнить без подтверждения пользователя.",
                            }
                        )
                        stop_after = "confirm"
                        continue
                    auto_ids += 1
                    label = None
                    if call.name == "browser_click" and isinstance(args.get("ref"), int):
                        label = guard.elements.get(args["ref"])
                    ask_args = {"action": call.name, "details": describe(call.name, args, label)}
                    answer = run_call(f"{call.id}_confirm{auto_ids}", CONFIRM_TOOL, ask_args)
                    calls_used += 1
                    if not approved(answer):
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": "Не выполнено: пользователь не разрешил это действие.",
                            }
                        )
                        stop_after = "confirm"
                        continue
                result = run_call(call.id, call.name, args)
                calls_used += 1
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.content})
                if key and result.error:
                    failed[key] = result.error
                guard.remember_page(result.content)
                if call.name == CONFIRM_TOOL:
                    ok = approved(result)
                    guard.record_approval(args, ok)
                    if not ok:
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
