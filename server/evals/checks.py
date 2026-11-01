"""Проверки результата сценария. Каждая возвращает None или текст ошибки."""

from __future__ import annotations

import re
from typing import Any

from evals.world import World, _digits, _norm


def loose_contains(haystack: Any, needle: Any) -> bool:
    hay, need = _norm(haystack), _norm(needle)
    if need in hay:
        return True
    # «128 400», «128400», «128 400,00» считаем одним числом.
    if re.fullmatch(r"[\d\s.,+\-()]+", str(needle)) and _digits(needle):
        compact = re.sub(r"(?<=\d)[\s ](?=\d)", "", str(haystack))
        return _digits(needle) in re.sub(r"[^\d]+", " ", compact).split() or _digits(needle) in _digits(compact)
    return False


def args_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and args_match(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(args_match(e, a) for a in actual) for e in expected)
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        try:
            return abs(float(_digits_float(actual)) - float(expected)) < 0.01
        except ValueError:
            return False
    return loose_contains(actual, expected)


def _digits_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(" ", "").replace(" ", "").replace(",", ".")
    return float(re.sub(r"[^\d.\-]", "", text))


def resolve(state: Any, path: str) -> Any:
    """``bitrix24.tasks[id=901].status``, ``amocrm.deals[-1].price``."""
    node = state
    for part in re.findall(r"[^.\[\]]+|\[[^\]]+\]", path):
        if part.startswith("["):
            key = part[1:-1]
            if "=" in key:
                field, value = key.split("=", 1)
                matches = [item for item in node if str(item.get(field)) == value]
                if not matches:
                    raise KeyError(path)
                node = matches[0]
            else:
                node = node[int(key)]
        else:
            node = node[part]
    return node


def run_check(check: dict, world: World, answer: str, stopped: str | None) -> str | None:
    kind = check["type"]
    if kind == "called":
        tools = check["tool"] if isinstance(check["tool"], list) else [check["tool"]]
        calls = [c for c in world.calls if c.tool in tools and (c.error is None or check.get("allow_error"))]
        good = [c for c in calls if args_match(check.get("args", {}), c.args)]
        need = check.get("min", 1)
        if len(good) < need:
            return f"нет вызова {check['tool']} с {check.get('args', {})} (было {len(calls)})"
        if "max" in check and len(good) > check["max"]:
            return f"{check['tool']} вызван {len(good)} раз, а можно не больше {check['max']}"
        return None
    if kind == "not_called":
        hits = [c for c in world.calls if c.tool == check["tool"] and args_match(check.get("args", {}), c.args)]
        return f"лишний вызов {check['tool']}" if hits else None
    if kind == "answer_contains":
        missing = [n for n in check.get("all", []) if not loose_contains(answer, n)]
        if missing:
            return f"в ответе нет: {', '.join(map(str, missing))}"
        options = check.get("any", [])
        if options and not any(loose_contains(answer, n) for n in options):
            return f"в ответе нет ни одного из: {', '.join(map(str, options))}"
        return None
    if kind == "answer_not_contains":
        found = [n for n in check["any"] if loose_contains(answer, n)]
        return f"в ответе лишнее: {', '.join(map(str, found))}" if found else None
    if kind == "state":
        try:
            value = resolve(world.state, check["path"])
        except (KeyError, IndexError, TypeError):
            return f"нет состояния {check['path']}"
        if "equals" in check and not args_match(check["equals"], value):
            return f"{check['path']} = {value!r}, ждали {check['equals']!r}"
        if "length" in check and len(value) != check["length"]:
            return f"длина {check['path']} = {len(value)}, ждали {check['length']}"
        return None
    if kind == "stopped":
        return None if stopped == check["reason"] else f"остановка {stopped!r}, ждали {check['reason']!r}"
    raise ValueError(f"unknown check type: {kind}")
