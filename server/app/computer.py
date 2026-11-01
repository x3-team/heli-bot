"""PC hands: the vision step of the use_computer tool.

The server never touches the user's screen. The desktop app sends a screenshot,
this module asks the vision model for one next action, checks it, and returns it.
The desktop shows a confirmation card when needed and performs the action.

Coordinates use a 0..1000 grid over the screenshot, so the model never needs the
real resolution and the desktop maps them onto the screen itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import HeliClient
from app.llm.router import choose_alias
from app.models import User
from app.quota import metered_complete, remaining_ratio, snapshot
from app.trajectories import add_step

GRID = 1000
MAX_STEPS = 30
TEXT_LIMIT = 2000
SCREENSHOT_LIMIT = 2_000_000
HISTORY_LIMIT = 12
_MAX_TOKENS = 400

# Tool the agent loop offers to the model. The loop hands the call to the desktop
# as a client action; the desktop drives the steps below and returns a summary.
COMPUTER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "use_computer",
        "description": (
            "Выполнить задачу на компьютере пользователя: в открытых программах Windows "
            "(1С, Битрикс24, Excel, проводник, любые окна). Heli смотрит на экран и кликает "
            "или печатает, пользователь подтверждает действия. Опиши цель одной фразой: что "
            "сделать и как понять, что готово. Для сайтов сначала пробуй open_url или браузер."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Что сделать на экране, например «В 1С открыть счёт 123 и нажать Провести»",
                },
            },
            "required": ["goal"],
        },
    },
}

POINT_ACTIONS = {"click", "double_click", "right_click"}
ACTIONS = POINT_ACTIONS | {"type", "key", "scroll", "wait", "done", "fail"}
_MODIFIERS = {"ctrl", "control", "alt", "shift", "win", "meta", "cmd", "super"}
_NAMED = {
    "enter", "return", "tab", "esc", "escape", "backspace", "delete", "del", "up", "down",
    "left", "right", "home", "end", "pageup", "pagedown", "space",
} | {f"f{n}" for n in range(1, 13)}

# Words that make a click or input irreversible or outward-facing. The user always
# confirms these, even with confirmations for ordinary clicks turned off.
_DANGER = re.compile(
    r"оплат|заплат|плат[её]ж|перев[её]|купить|покуп|заказ|удал|стереть|очист|отправ|"
    r"провести|проведени|подпис|утверд|опубликов|уволь|выйти из|"
    r"\bpay\b|payment|purchase|\bbuy\b|delete|remove|\bsend\b|submit|publish|sign",
    re.IGNORECASE,
)

_SYSTEM = (
    "Ты Heli и управляешь компьютером пользователя на Windows через скриншоты. "
    "Тебе дают цель, историю уже сделанных шагов и свежий скриншот главного экрана. "
    "Верни ровно одно следующее действие в JSON без пояснений вокруг:\n"
    '{"thought":"что видишь и зачем шаг","action":"click","x":0,"y":0,'
    '"target":"что именно на экране, по-русски","text":null,"keys":null,'
    '"amount":null,"seconds":null,"danger":false,"summary":null}\n'
    "Координаты x и y — центр элемента в сетке 0..1000 по ширине и высоте скриншота "
    "(0,0 — левый верхний угол, 1000,1000 — правый нижний).\n"
    "action: click, double_click, right_click — нужны x, y и target. "
    "type — text (и x, y, если сначала надо кликнуть в поле). "
    "key — keys, например enter, tab, ctrl+s, alt+tab. "
    "scroll — x, y и amount от -15 до 15 (минус — вверх). "
    "wait — seconds от 1 до 10, когда программа грузится. "
    "done — цель достигнута, в summary коротко итог. "
    "fail — дальше нельзя без человека (вход, капча, непонятная ошибка, нужна информация), "
    "в summary что мешает.\n"
    "danger=true, если шаг отправляет, оплачивает, удаляет, проводит или подписывает документ, "
    "или его нельзя отменить. Не вводи пароли и коды: для этого верни fail. "
    "Не нажимай то, чего не просили. Если прошлый шаг не сработал, попробуй иначе, "
    "а после трёх неудач подряд верни fail."
)


class ComputerError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class HistoryItem:
    action: str
    target: str | None = None
    ok: bool = True
    note: str | None = None


@dataclass
class Step:
    action: dict
    thought: str
    risk: str
    alias: str
    step_id: str
    cost_rub: Decimal = Decimal("0")
    finished: bool = False
    error: str | None = None


def pc_hands_enabled() -> bool:
    """Feature flag HELI_PC_HANDS. Off by default: v0.1 works in the cloud computer only."""
    return get_settings().pc_hands


def decode_screenshot(raw: str) -> tuple[bytes, str]:
    data = raw.split(",", 1)[-1]
    try:
        blob = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ComputerError("bad_screenshot") from None
    if not blob or len(blob) > SCREENSHOT_LIMIT:
        raise ComputerError("bad_screenshot")
    if blob.startswith(b"\x89PNG"):
        return blob, "image/png"
    if blob.startswith(b"\xff\xd8"):
        return blob, "image/jpeg"
    raise ComputerError("bad_screenshot")


def vision_messages(goal: str, history: list[HistoryItem], image: bytes, mime: str, step: int) -> list[dict]:
    lines = []
    for index, item in enumerate(history[-HISTORY_LIMIT:], 1):
        status = "ок" if item.ok else "не вышло"
        line = f"{index}. {item.action}"
        if item.target:
            line += f" — {item.target}"
        line += f" ({status})"
        if item.note:
            line += f": {item.note[:200]}"
        lines.append(line)
    done = "\n".join(lines) if lines else "пока ничего"
    text = f"Цель: {goal}\nШаг {step} из {MAX_STEPS}.\nУже сделано:\n{done}\nСкриншот ниже. Какой следующий шаг?"
    url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    return [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        },
    ]


def parse_action(text: str) -> tuple[dict, str, bool]:
    """Model reply -> (action for the desktop, thought, flagged as dangerous)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ComputerError("bad_model_reply")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise ComputerError("bad_model_reply") from None
    if not isinstance(data, dict):
        raise ComputerError("bad_model_reply")
    kind = str(data.get("action") or "").strip().lower()
    if kind not in ACTIONS:
        raise ComputerError("bad_action")
    thought = str(data.get("thought") or "")[:500]
    target = _clean(data.get("target"), 200)
    action: dict = {"type": kind}
    if kind in POINT_ACTIONS or kind == "scroll":
        action["x"], action["y"] = _point(data)
    if kind == "type":
        text_value = data.get("text")
        if not isinstance(text_value, str) or not text_value or len(text_value) > TEXT_LIMIT:
            raise ComputerError("bad_action")
        action["text"] = text_value
        if data.get("x") is not None and data.get("y") is not None:
            action["x"], action["y"] = _point(data)
    if kind == "key":
        action["keys"] = normalize_keys(data.get("keys"))
    if kind == "scroll":
        action["amount"] = _bounded_int(data.get("amount"), -15, 15, zero_ok=False)
    if kind == "wait":
        action["seconds"] = _bounded_int(data.get("seconds"), 1, 10, zero_ok=False)
    if kind in {"done", "fail"}:
        action["summary"] = _clean(data.get("summary"), 1000) or ("Готово" if kind == "done" else "Нужна помощь человека")
    if target:
        action["target"] = target
    flagged = bool(data.get("danger")) or looks_dangerous(target, action.get("text"))
    return action, thought, flagged


def normalize_keys(raw) -> str:
    if not isinstance(raw, str):
        raise ComputerError("bad_action")
    parts = [part.strip().lower() for part in raw.split("+")]
    if not parts or len(raw) > 40:
        raise ComputerError("bad_action")
    *mods, key = parts
    if len(set(mods)) != len(mods) or any(mod not in _MODIFIERS for mod in mods):
        raise ComputerError("bad_action")
    if key not in _NAMED and not (len(key) == 1 and key.isascii() and key.isalnum()):
        raise ComputerError("bad_action")
    return "+".join(parts)


def dangerous_keys(keys: str) -> bool:
    *mods, key = keys.split("+")
    mods_set = {"win" if m in {"meta", "cmd", "super"} else "ctrl" if m == "control" else m for m in mods}
    if "win" in mods_set:
        return True
    if key == "f4" and "alt" in mods_set:
        return True
    if key in {"delete", "del"} and ("shift" in mods_set or {"ctrl", "alt"} <= mods_set):
        return True
    if key in {"esc", "escape"} and {"ctrl", "shift"} <= mods_set:
        return True
    return key in {"w", "q"} and "ctrl" in mods_set


def looks_dangerous(*texts: str | None) -> bool:
    return any(text and _DANGER.search(text) for text in texts)


def risk_of(action: dict, flagged: bool) -> str:
    """Mirrors local_agent::actions::ComputerAction::risk on the desktop."""
    kind = action["type"]
    if kind in {"done", "fail"}:
        return "read"
    if flagged:
        return "danger"
    if kind in {"wait", "scroll"}:
        return "read"
    if kind == "key" and dangerous_keys(action["keys"]):
        return "danger"
    return "write"


def next_step(
    session: Session,
    user: User,
    task_id: uuid.UUID,
    client: HeliClient,
    *,
    goal: str,
    screenshot_base64: str,
    history: list[HistoryItem],
    step: int,
) -> Step:
    if not pc_hands_enabled():
        raise ComputerError("pc_hands_disabled")
    goal = goal.strip()
    if not goal:
        raise ComputerError("bad_goal")
    if step > MAX_STEPS:
        raise ComputerError("too_many_steps")
    image, mime = decode_screenshot(screenshot_base64)
    failures = 0
    for item in reversed(history):
        if item.ok:
            break
        failures += 1
    alias = choose_alias(
        phase="execute",
        complexity=2,
        needs_vision=True,
        consecutive_errors=failures,
        quota_remaining=remaining_ratio(snapshot(session, user)),
    )
    started = time.monotonic()
    reply = metered_complete(
        session,
        user,
        task_id,
        client,
        alias,
        vision_messages(goal, history, image, mime, step),
        max_tokens=_MAX_TOKENS,
    )
    latency = int((time.monotonic() - started) * 1000)
    error = None
    try:
        action, thought, flagged = parse_action(reply.text)
    except ComputerError as exc:
        error = exc.code
        action, thought, flagged = {"type": "wait", "seconds": 1}, "", False
    risk = risk_of(action, flagged)
    row = add_step(
        session,
        user,
        task_id,
        thought=thought or "экран",
        plan=goal[:500],
        model_alias=reply.alias,
        reply=reply.text[:2000],
        tool_name=f"computer.{action['type']}",
        tool_args={**action, "risk": risk},
        observation_type="screenshot",
        screenshot=image,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        cost_rub=reply.cost_rub,
        latency_ms=latency,
        error=error,
    )
    return Step(
        action=action,
        thought=thought,
        risk=risk,
        alias=reply.alias,
        step_id=str(row.id),
        cost_rub=reply.cost_rub,
        finished=action["type"] in {"done", "fail"},
        error=error,
    )


def _point(data: dict) -> tuple[int, int]:
    x = _bounded_int(data.get("x"), 0, GRID)
    y = _bounded_int(data.get("y"), 0, GRID)
    return x, y


def _bounded_int(value, low: int, high: int, *, zero_ok: bool = True) -> int:
    if isinstance(value, bool) or value is None:
        raise ComputerError("bad_action")
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise ComputerError("bad_action") from None
    if number < low or number > high or (number == 0 and not zero_ok):
        raise ComputerError("bad_action")
    return number


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None
