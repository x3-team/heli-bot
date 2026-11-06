"""One policy for changes in the user's accounts: read freely, write to sandboxes, write live only after a «да».

Every tool that can change something in a service (Битрикс24, Директ, http_request, amoCRM, GA4, …)
registers a classifier here. For each call the classifier says what the call is:

    read     — reads only. Runs without a question, live account or not.
    sandbox  — writes to a known sandbox or training host (b24-lvsc4f, api-sandbox.direct.yandex.com,
               HELI_*_SANDBOX_HOSTS, a training GA4 ID, a trial amoCRM). Runs without a question,
               except the dangerous verbs the guard already stops (send, pay, delete, …).
    live     — writes anywhere else: a client's portal, api.direct.yandex.com, a live Measurement ID.
               Runs only after an approved ask_confirmation on the card this module writes (host or
               object, what changes, and for ads or payments a warning that money may be spent).

Two locks, so neither the model nor a bug in one place is enough to change a live account:

1. The loop (app.agent.guard) asks before a live write and shows this module's card. The model's own
   ask_confirmation does not count for a live write: its text may leave out the host or the amounts.
   Without a person to ask (scheduled runs) the call is refused.
2. The executor checks `confirmed(args)` before the network. The loop sets CONFIRMED_ARG only after
   the user approved this very call, and strips any "_…" argument the model writes itself.

How an integration plugs in (amocrm_write, ga4 Measurement Protocol, …):

    from app.agent import write_policy as wp

    def classify(args: dict) -> wp.Write:
        host = ...                                  # from args only: no network, no vault
        if is_read(args):
            return wp.read(host)
        if host in wp.hosts_from_env("HELI_AMO_SANDBOX_HOSTS", ("heli-trial.amocrm.ru",)):
            return wp.sandbox(host, "создать сделку «…»")
        return wp.live(host, "создать сделку «…» на 15 000 ₽", budget=False)

    wp.register("amocrm_write", classify)           # at import / register() time

    # in the executor, right before the request:
    decision = wp.classify("amocrm_write", args)
    if decision.kind == wp.LIVE and not wp.confirmed(args):
        return ClientResult(wp.REFUSED_TEXT, wp.REFUSED_CODE)

A tool name with no classifier is not touched by this module; the guard's name rules still apply.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

READ = "read"
SANDBOX = "sandbox"
LIVE = "live"

# Set by the loop on the arguments of a call the user approved. The model cannot set it: the loop
# drops every argument that starts with "_" before adding this one.
CONFIRMED_ARG = "_confirmed"
REFUSED_CODE = "confirmation_required"
REFUSED_TEXT = (
    "Не выполнено: это изменение в боевом кабинете, а пользователь его не подтвердил. Ничего не отправлено. "
    "Запись в бою идёт только после «да» в карточке подтверждения."
)
BUDGET_WARNING = "Внимание: действие может потратить рекламный бюджет или деньги клиента."
CARD_TITLE = "Изменение в боевом кабинете"


@dataclass(frozen=True)
class Write:
    """What one call does. `where` is the host or object, `what` the change in plain words."""

    kind: str
    where: str = ""
    what: str = ""
    budget: bool = False
    # The API method or verb (crm.deal.add, delete, POST /v1/send). In a sandbox the guard still asks
    # when it is a dangerous verb (send, pay, delete, …), as it does for tool names.
    verb: str = ""


def read(where: str = "") -> Write:
    return Write(READ, where)


def sandbox(where: str, what: str = "", verb: str = "") -> Write:
    return Write(SANDBOX, where, what, verb=verb)


def live(where: str, what: str, budget: bool = False, verb: str = "") -> Write:
    return Write(LIVE, where or "адрес не указан", what, budget, verb)


Classifier = Callable[[dict], Write]
_RULES: dict[str, Classifier] = {}


def register(tool: str, classifier: Classifier) -> None:
    _RULES[tool] = classifier


def unregister(tool: str) -> None:
    _RULES.pop(tool, None)


def has_rule(tool: str) -> bool:
    _load_builtin()
    return tool in _RULES


# Built-in tools whose modules register their rule on import. Loaded on first use, so the guard
# knows them even in a process that never called their register().
_BUILTIN = ("app.agent.http_tool", "app.agent.bitrix", "app.agent.yandex_direct", "app.agent.amocrm")
_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib

    for module in _BUILTIN:
        importlib.import_module(module)


def classify(tool: str, args: dict) -> Write | None:
    """None when the tool has no rule. A classifier that fails treats the call as live: ask, don't guess."""
    _load_builtin()
    rule = _RULES.get(tool)
    if rule is None:
        return None
    clean = {k: v for k, v in (args or {}).items() if not str(k).startswith("_")}
    try:
        return rule(clean)
    except Exception:  # noqa: BLE001
        return live("", f"{tool}: не удалось разобрать вызов")


def needs_confirmation(tool: str, args: dict) -> bool:
    decision = classify(tool, args)
    return decision is not None and decision.kind == LIVE


def confirmed(args: dict | None) -> bool:
    return bool(args) and args.get(CONFIRMED_ARG) is True


def card(tool: str, args: dict) -> dict | None:
    """ask_confirmation arguments for a live write: where, what, and the budget warning when money may go."""
    decision = classify(tool, args)
    if decision is None or decision.kind != LIVE:
        return None
    lines = [f"{CARD_TITLE}: {decision.where}.", f"Что изменится: {decision.what or tool}."]
    if decision.budget:
        lines.append(BUDGET_WARNING)
    return {"action": tool, "details": " ".join(lines)[:1000]}


def hosts_from_env(env: str, defaults: Iterable[str] = ()) -> set[str]:
    """Sandbox hosts: the built-in list plus a comma-separated env variable (HELI_*_SANDBOX_HOSTS)."""
    extra = os.environ.get(env, "")
    return {h.strip().lower() for h in (*defaults, *extra.split(",")) if h.strip()}


def _hide_secrets(value):
    from app.masking import SECRET_KEYS

    if isinstance(value, dict):
        return {k: ("<CREDENTIAL>" if str(k).lower() in SECRET_KEYS and v else _hide_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_hide_secrets(v) for v in value]
    return value


def short(value, limit: int = 300) -> str:
    """A value for the card, cut to fit, with secret fields and passwords masked. Callers pass args
    before {{secret:…}} is filled in, so the card shows the placeholder, never the key."""
    import json

    from app.masking import mask_passwords

    text = value if isinstance(value, str) else json.dumps(_hide_secrets(value), ensure_ascii=False, default=str)
    text = mask_passwords(text)
    return text if len(text) <= limit else text[:limit] + "…"
