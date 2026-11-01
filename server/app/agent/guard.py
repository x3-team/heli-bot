"""Server-side gate for actions that are hard to undo.

The model is told to call ask_confirmation first, but the loop does not rely on that:
a risky tool runs only after an approved ask_confirmation that covers it. If there is
none, the loop asks itself. Without approval the tool is never executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tool names that send, pay, delete or publish. Integrations may also mark a schema
# with "x-heli-confirm": true.
_RISKY_NAME = re.compile(
    r"(^|_)(send|reply|forward|delete|remove|destroy|pay|payment|invoice|transfer|publish|post|submit|cancel|refund)(_|$)"
)
# Buttons in the user's browser that do the same.
_RISKY_LABEL = re.compile(
    r"отправ|оплат|заплат|удал|опубликов|перевест|перевод|списат|подписат|подтверд|выставить сч|send|pay|delete|remove|publish|submit",
    re.I,
)
_ELEMENT = re.compile(r"^\[(\d+)\]\s+(.*)$")


def risky_tool(name: str, schema: dict | None = None) -> bool:
    if schema and (schema.get("x-heli-confirm") or schema.get("function", {}).get("x-heli-confirm")):
        return True
    if name.startswith("browser_") or name in {"ask_confirmation", "ask_user", "web_search", "open_url"}:
        return False
    return bool(_RISKY_NAME.search(name))


@dataclass
class Guard:
    """Tracks approvals within one run. One approval covers one risky action."""

    schemas: dict[str, dict] = field(default_factory=dict)
    by_tool: dict[str, int] = field(default_factory=dict)
    generic: int = 0
    elements: dict[int, str] = field(default_factory=dict)

    def remember_page(self, text: str) -> None:
        """Keep element labels from the last page read, to judge browser clicks by ref."""
        if "Элементы:" not in text:
            return
        self.elements = {}
        for line in text.split("Элементы:", 1)[1].splitlines():
            match = _ELEMENT.match(line.strip())
            if match:
                self.elements[int(match.group(1))] = match.group(2)

    def needs_approval(self, name: str, args: dict) -> bool:
        if name == "browser_click":
            label = str(args.get("text") or "")
            if not label and isinstance(args.get("ref"), int):
                label = self.elements.get(args["ref"], "")
            return bool(label) and bool(_RISKY_LABEL.search(label))
        if name == "browser_type" and args.get("submit"):
            return False  # submitting a search box is fine; forms are sent with a button
        return risky_tool(name, self.schemas.get(name))

    def record_approval(self, args: dict, approved_: bool) -> None:
        if not approved_:
            return
        action = str(args.get("action") or "").strip()
        if action in self.schemas or risky_tool(action):
            self.by_tool[action] = self.by_tool.get(action, 0) + 1
        else:
            self.generic += 1

    def consume(self, name: str) -> bool:
        if self.by_tool.get(name, 0) > 0:
            self.by_tool[name] -= 1
            return True
        if self.generic > 0:
            self.generic -= 1
            return True
        return False


def describe(name: str, args: dict, label: str | None = None) -> str:
    if label:
        return f"Нажать «{label}» в браузере"
    parts = []
    for key, value in args.items():
        text = str(value)
        parts.append(f"{key}: {text[:200]}")
    return "; ".join(parts)[:800]
