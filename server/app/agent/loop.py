"""The agent loop: ask the model, run the tools it calls, feed results back, repeat.

It knows nothing about the database or HTTP. The caller passes:
- complete(messages, tools) -> Completion   (metered model call)
- call_tool(call_id, name, args) -> ToolResult
- on_event(dict)                            (step stream + trajectory)
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.agent.control import Control, NoControl, Stopped
from app.agent.guard import Guard, describe
from app.llm.client import Completion
from app.llm.tools import parse_args
from app.masking import SECRET_KEYS, mask_passwords

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


BROKEN_NOTE = (
    "Твой прошлый ответ не распознан: в нём разметка вызова инструмента вместо вызова. "
    "Вызови инструмент обычным способом или ответь пользователю текстом."
)
BUDGET_TEXT = "Остановился: задача дошла до предела расходов на один запуск."
GAVE_UP = "Не успел закончить задачу за отведённые шаги."
FINISH_NOTE = (
    "Лимит действий исчерпан. Больше инструменты не вызывай: ответь пользователю тем, что уже нашёл, "
    "с конкретными цифрами и источниками, и скажи, что не успел."
)


LOOP_NOTE = (
    "Ты несколько раз повторил одни и те же действия с одним и тем же результатом. Больше инструменты "
    "не вызывай: честно скажи пользователю, что не получилось, что именно ты пробовал и что видел, "
    "и что можно сделать дальше."
)
REPEAT_WARNING = (
    "\n\n[Heli] Этот вызов уже второй раз дал тот же результат. Не повторяй его: смени тактику "
    "(другой адрес или элемент, browser_screenshot и клик по координатам, другой раздел) "
    "или закончи и честно скажи, что не получилось."
)
REPEAT_BLOCKED = (
    "Не выполнено: этот вызов уже дважды дал один и тот же результат, повтор ничего не изменит. "
    "Смени тактику или закончи и честно скажи пользователю, что не получилось."
)
# A third identical call is refused; this many refusals end the run with a summary.
BLOCKS_TO_STOP = 2
# Looking does not change the page; an action in between makes looking again worthwhile.
_LOOK_TOOLS = ("browser_read", "browser_screenshot")
# Tools that load the same thing again whatever the page shows: same args = same call.
_REOPEN_TOOLS = {"browser_open", "open_url", "web_search"}
SCREEN_NOTE = (
    "Снимок экрана после browser_screenshot ({w}×{h}). Для browser_click по координатам бери x и y "
    "в пикселях этого снимка, от левого верхнего угла."
)
SCREEN_DROPPED = "[снимок экрана был здесь, модель его уже видела]"
SCREEN_SIZE = (1280, 800)

# DeepSeek writes its tool-call markup into the text when it cannot call tools in this round.
_DSML_BLOCK = re.compile(
    r"<[｜|]\s*DSML\s*[｜|]\s*(tool_calls|function_calls)\s*>.*?(?:</[｜|]\s*DSML\s*[｜|]\s*\1\s*>|$)", re.S
)
_TOOL_TOKENS = re.compile(r"<[｜|]tool▁calls▁begin[｜|]>.*?(?:<[｜|]tool▁calls▁end[｜|]>|$)", re.S)
_SPECIAL = re.compile(r"</?[｜|]\s*DSML\s*[｜|][^>]*>|<[｜|][^<>｜|]{1,40}[｜|]>")


def strip_markup(text: str) -> str:
    """The answer without the model's raw tool-call markup."""
    if not text:
        return text
    cleaned = _DSML_BLOCK.sub("", text)
    cleaned = _TOOL_TOKENS.sub("", cleaned)
    cleaned = _SPECIAL.sub("", cleaned)
    return cleaned.strip()


def has_markup(text: str) -> bool:
    return bool(text) and strip_markup(text) != text.strip()


def call_key(name: str, args: dict) -> str:
    """Identity of a call for the cycle check: the tool and all its arguments."""
    clean = {k: (v.strip().lower() if isinstance(v, str) else v) for k, v in args.items() if not k.startswith("_")}
    return name + json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)


def result_hash(result: "ToolResult") -> str:
    text = re.sub(r"\s+", " ", result.content or "").strip()
    digest = hashlib.sha1(f"{result.error}|{text}".encode())
    shot = result.data.get("screenshot") if isinstance(result.data, dict) else None
    if isinstance(shot, (bytes, bytearray)):
        digest.update(bytes(shot))
    return digest.hexdigest()


def summary_messages(messages: list[dict], note: str, limit: int = 14000) -> list[dict]:
    """The run so far as plain text, without tool calls: any model can answer it with words only.

    Sending the real history without tools makes some models print tool-call markup instead."""
    system = [m for m in messages[:1] if m.get("role") == "system"]
    lines: list[str] = []
    for message in messages[len(system):]:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = strip_markup(str(content or ""))
        if role == "user":
            lines.append(f"Пользователь: {content[:3000]}")
        elif role == "assistant":
            if content:
                lines.append(f"Ты: {content[:1500]}")
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                lines.append(f"Ты вызвал {fn.get('name')}({str(fn.get('arguments', ''))[:300]})")
        elif role == "tool":
            lines.append(f"Результат: {content[:1200]}")
    transcript = "\n".join(lines)
    if len(transcript) > limit:
        transcript = "…\n" + transcript[-limit:]
    return [
        *system,
        {"role": "user", "content": f"Ход работы над задачей:\n{transcript}\n\n{note}"},
    ]


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
    seen: dict[str, list[str]] = {}  # call -> hashes of the results it gave
    blocked = 0
    broken_rounds = 0
    finish_note: str | None = None  # set: the next round is the last, words only
    finish_reason: str | None = None

    def safe_args(name: str, args: dict) -> dict:
        """Arguments as they go to the step stream and the trajectory: no passwords, no internals."""
        clean = {
            k: ("<CREDENTIAL>" if str(k).lower() in SECRET_KEYS and v else v)
            for k, v in args.items()
            if not k.startswith("_")
        }
        if name == "browser_type" and isinstance(clean.get("text"), str) and "<CREDENTIAL:" not in clean["text"]:
            label = guard.elements.get(clean["ref"], "") if isinstance(clean.get("ref"), int) else ""
            if label.startswith("поле пароля") or args.get("_secret"):
                clean["text"] = "<CREDENTIAL>"
        return {k: (mask_passwords(v) if isinstance(v, str) else v) for k, v in clean.items()}

    def run_call(call_id: str, name: str, args: dict) -> ToolResult:
        shown = safe_args(name, args)
        emit({"type": "step", "kind": "tool_call", "call_id": call_id, "tool": name, "args": shown})
        result = call_tool(call_id, name, args)
        content = result.content[:TOOL_RESULT_LIMIT]
        emit(
            {
                "type": "step",
                "kind": "tool_result",
                "call_id": call_id,
                "tool": name,
                "args": shown,
                "text": content,
                "error": result.error,
                "data": result.data,
            }
        )
        return ToolResult(content, result.error, result.data)

    def drop_old_screens() -> None:
        """A screenshot is shown to the model once: later rounds get a note instead (cost, and the text model)."""
        for index, message in enumerate(messages):
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in content
            ):
                messages[index] = {"role": "user", "content": SCREEN_DROPPED}

    try:
        round_index = 0
        while True:
            control.checkpoint()
            if budget.max_rub is not None and spent >= budget.max_rub:
                out.stopped = "budget"
                out.final_answer = BUDGET_TEXT
                break
            if budget.autopause and threshold is not None and spent > threshold:
                control.auto_pause(
                    "cost",
                    {"spent_rub": str(spent), "estimate_rub": str(budget.estimate_rub)},
                )
                # Resumed: pause again if the run costs another half of what it has spent.
                threshold = spent * AUTOPAUSE_RATIO
            out_of_calls = budget.max_tool_calls is not None and calls_used >= budget.max_tool_calls
            out_of_steps = round_index >= budget.max_steps - 1
            if finish_note is None and (out_of_steps or out_of_calls):
                finish_note = FINISH_NOTE
                finish_reason = "max_steps" if out_of_steps and not out_of_calls else None
            last = finish_note is not None
            if last:
                # Words only: the run as plain text, no tools. With the real history and no tools
                # some models print their tool-call markup instead of an answer.
                completion = complete(summary_messages(messages, finish_note), None)
            else:
                completion = complete(messages, tools)
            drop_old_screens()
            round_index += 1
            out.steps += 1
            spent += completion.cost_rub
            out.alias = completion.alias
            out.input_tokens += completion.input_tokens
            out.output_tokens += completion.output_tokens
            text = strip_markup(completion.text)
            broken = has_markup(completion.text) and not completion.tool_calls
            emit(
                {
                    "type": "step",
                    "kind": "answer" if (not completion.tool_calls or last) else "model",
                    "model_alias": completion.alias,
                    "text": mask_passwords(text),
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "cost_rub": completion.cost_rub,
                    "spent_rub": spent,
                }
            )
            if last:
                out.final_answer = text or GAVE_UP
                out.stopped = out.stopped or finish_reason
                break
            if broken and not text:
                # Markup instead of a real call or an answer. Once: ask again; twice: words only.
                broken_rounds += 1
                if broken_rounds >= 2:
                    finish_note = FINISH_NOTE
                    finish_reason = None
                else:
                    messages.append({"role": "user", "content": BROKEN_NOTE})
                continue
            if not completion.tool_calls:
                out.final_answer = text
                break
            completion.text = text
            messages.append(completion.assistant_message())
            stop_after = None
            screens: list[bytes] = []
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
                ckey = call_key(call.name, args)
                past = seen.get(ckey, [])
                if call.name != CONFIRM_TOOL and len(past) >= 2 and (
                    call.name in _REOPEN_TOOLS or past[-1] == past[-2]
                ):
                    # Third time the same call, and twice it changed nothing: refuse, don't loop.
                    blocked += 1
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": REPEAT_BLOCKED})
                    emit({"type": "step", "kind": "repeat_blocked", "tool": call.name, "args": safe_args(call.name, args)})
                    continue
                approved_here = False
                if guard.needs_approval(call.name, args):
                    if guard.consume(call.name):
                        approved_here = True
                    elif not can_ask:
                        # The model skipped ask_confirmation: the loop asks, and never runs the tool unasked.
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": "Ошибка: это действие нельзя выполнить без подтверждения пользователя.",
                            }
                        )
                        stop_after = "confirm"
                        continue
                    else:
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
                        approved_here = True
                run_args = {k: v for k, v in args.items() if not k.startswith("_")}
                if approved_here and call.name == "browser_click":
                    # The browser refuses a risky click by coordinates unless the loop says it was approved.
                    run_args["_confirmed"] = True
                result = run_call(call.id, call.name, run_args)
                calls_used += 1
                content = result.content
                if call.name != CONFIRM_TOOL:
                    digest = result_hash(result)
                    if past and past[-1] == digest:
                        content += REPEAT_WARNING
                    seen[ckey] = [*past, digest]
                    if call.name not in _LOOK_TOOLS:
                        # Something was done: looking again may show a new page.
                        for other in [k for k in seen if k.startswith(_LOOK_TOOLS)]:
                            seen.pop(other)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if key and result.error:
                    failed[key] = result.error
                guard.remember_page(result.content)
                shot = result.data.get("screenshot") if isinstance(result.data, dict) else None
                if call.name == "browser_screenshot" and isinstance(shot, (bytes, bytearray)) and shot:
                    screens.append(bytes(shot))
                if call.name == CONFIRM_TOOL:
                    ok = approved(result)
                    guard.record_approval(args, ok)
                    if not ok:
                        stop_after = "confirm"
            if screens:
                # Tool messages carry text only: the picture goes in a user message right after them.
                w, h = SCREEN_SIZE
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": SCREEN_NOTE.format(w=w, h=h)},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(screens[-1]).decode()},
                            },
                        ],
                    }
                )
            if stop_after:
                out.stopped = stop_after
                out.final_answer = "Остановился: действие не подтверждено."
                break
            if blocked >= BLOCKS_TO_STOP and finish_note is None:
                finish_note = LOOP_NOTE
                finish_reason = "loop"
                out.stopped = "loop"
    except Stopped as exc:
        out.stopped = exc.reason
    out.final_answer = mask_passwords(out.final_answer)
    out.cost_rub = spent
    return out


def has_image(messages: list[dict]) -> bool:
    """The next round must go to a model that sees pictures."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            return True
    return False
