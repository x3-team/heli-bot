"""Read-only МойСклад JSON API 1.2 for the agent: stock, products, counterparties, a saved document.

Actions (create, post, delete) still go through the screen; this tool only reads. It is for answers
that need exact numbers and for checking a document after «Сохранить» (did the article, quantity,
counterparty really save).

Access: the user's МойСклад login in «Доступы» (site moysklad.ru / online.moysklad.ru). If the login
is «token», the password is used as a Bearer token. Only GET requests to api.moysklad.ru are made.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid

import httpx

from app.agent.control import ClientResult

log = logging.getLogger(__name__)

API = "https://api.moysklad.ru/api/remap/1.2/"
TIMEOUT_S = 20
MAX_ROWS = 50
RESULT_CHARS = 8000

# What the model may read, and the path for it.
RESOURCES = {
    "stock": "report/stock/all",
    "product": "entity/product",
    "assortment": "entity/assortment",
    "counterparty": "entity/counterparty",
    "store": "entity/store",
    "organization": "entity/organization",
    "supply": "entity/supply",
    "customerorder": "entity/customerorder",
    "invoiceout": "entity/invoiceout",
    "demand": "entity/demand",
    "move": "entity/move",
    "inventory": "entity/inventory",
}
# Fields worth showing; the rest of a row (meta links, accounts) only costs tokens.
FIELDS = (
    "id", "name", "code", "article", "externalCode", "description", "inn", "kpp", "phone", "email",
    "actualAddress", "companyType", "stock", "reserve", "inTransit", "quantity", "minimumBalance",
    "price", "salePrice", "buyPrice", "uom", "moment", "created", "updated", "applicable", "sum",
    "vatSum", "payedSum", "shippedSum", "state", "agent", "store", "organization", "positions", "folder",
    "stockDays",
)
_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL = _fn(
    "moysklad_read",
    "Прочитать данные МоегоСклада через его API (только чтение): остатки, товары, контрагентов, документы. "
    "Используй для точных чисел (остатки, неснижаемый остаток, цены) и чтобы проверить документ или карточку "
    "после «Сохранить»: что поля действительно сохранились. Создавать и менять данные — только через экран.",
    {
        "resource": {"type": "string", "enum": sorted(RESOURCES), "description": "Что читать"},
        "id": {"type": "string", "description": "id записи (из адреса карточки …#good/edit?id=… или из списка)"},
        "search": {"type": "string", "description": "Контекстный поиск: название, артикул, ИНН"},
        "filter": {"type": "string", "description": "Фильтр API 1.2, например article=GL-M-100 или name=Ромашка"},
        "below_minimum": {"type": "boolean", "description": "Для stock: только товары ниже неснижаемого остатка"},
        "limit": {"type": "integer", "description": "Сколько строк, до 50"},
    },
    ["resource"],
)
TOOL_NAMES = ("moysklad_read",)


class MsError(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.text = text


def build_request(args: dict) -> tuple[str, dict]:
    """Path and query for the arguments. Never anything but a GET to an allowed resource."""
    resource = str(args.get("resource") or "")
    if resource not in RESOURCES:
        raise MsError("bad_resource", "Ошибка: такой раздел читать нельзя. Можно: " + ", ".join(sorted(RESOURCES)))
    path = RESOURCES[resource]
    params: dict = {}
    record = str(args.get("id") or "").strip()
    if record:
        if resource == "stock" or not _ID.match(record):
            raise MsError("bad_id", "Ошибка: id записи — это uuid из адреса карточки")
        path += "/" + record.lower()
        if resource in {"supply", "customerorder", "invoiceout", "demand", "move", "inventory"}:
            params["expand"] = "positions.assortment,agent,store"
        return path, params
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    params["limit"] = max(1, min(limit, MAX_ROWS))
    search = str(args.get("search") or "").strip()
    if search:
        params["search"] = search[:200]
    filters = [str(args.get("filter") or "").strip()[:300]]
    if resource == "stock" and args.get("below_minimum"):
        filters.append("stockMode=underMinimum")  # MoySklad: positions below the minimum balance
    filters = [f for f in filters if f]
    if filters:
        params["filter"] = ";".join(filters)
    return path, params


def _short(value):
    """A nested entity as its name (or the row as is)."""
    if isinstance(value, dict):
        if "name" in value:
            return value["name"]
        if "rows" in value and isinstance(value["rows"], list):
            return [compact(row) for row in value["rows"][:MAX_ROWS]]
        if "value" in value:  # salePrices[0].value etc.
            return value["value"]
        return None
    if isinstance(value, list):
        return [_short(item) for item in value[:MAX_ROWS]]
    return value


def compact(row: dict) -> dict:
    out = {}
    for key in FIELDS:
        if key in row and row[key] not in (None, "", [], {}):
            value = _short(row[key]) if key != "positions" else _positions(row[key])
            if value not in (None, "", [], {}):
                out[key] = value
    if "assortment" in row:
        out["assortment"] = _short(row["assortment"])
    if "salePrices" in row and isinstance(row["salePrices"], list):
        out["salePrices"] = [p.get("value") for p in row["salePrices"][:3] if isinstance(p, dict)]
    for money in ("price", "salePrice", "buyPrice", "sum", "vatSum", "payedSum", "shippedSum"):
        if isinstance(out.get(money), (int, float)):
            out[money] = round(out[money] / 100, 2)  # API gives kopecks
    return out


def _positions(value) -> list:
    rows = value.get("rows") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows[:MAX_ROWS]:
        item = {"assortment": _short(row.get("assortment")), "quantity": row.get("quantity")}
        if isinstance(row.get("price"), (int, float)):
            item["price"] = round(row["price"] / 100, 2)
        result.append(item)
    return result


def format_result(data: dict) -> str:
    if "rows" in data:
        rows = [compact(row) for row in data.get("rows") or []]
        size = (data.get("meta") or {}).get("size", len(rows))
        head = f"Найдено {size}, показано {len(rows)}."
        body = json.dumps(rows, ensure_ascii=False)
    else:
        head = "Запись:"
        body = json.dumps(compact(data), ensure_ascii=False)
    text = f"{head}\n{body}"
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "…"


def auth_header(username: str, secret: str) -> str:
    if username.strip().lower() in {"token", "bearer"}:
        return f"Bearer {secret}"
    return "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()


def fetch(path: str, params: dict, authorization: str, transport: httpx.BaseTransport | None = None) -> dict:
    with httpx.Client(base_url=API, timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.get(
            path,
            params=params,
            headers={"Authorization": authorization, "Accept-Encoding": "gzip", "Accept": "application/json;charset=utf-8"},
        )
    if response.status_code in (401, 403):
        raise MsError("auth_failed", "Ошибка: МойСклад не принял логин из «Доступов» (нужен доступ к API у пользователя).")
    if response.status_code == 404:
        raise MsError("not_found", "Ошибка: такой записи в МоёмСкладе нет.")
    if response.status_code >= 400:
        detail = ""
        try:
            detail = "; ".join(e.get("error", "") for e in response.json().get("errors", [])[:3])
        except Exception:  # noqa: BLE001
            pass
        raise MsError("api_error", f"Ошибка API МоегоСклада {response.status_code}: {detail}"[:500])
    return response.json()


def execute(session, user, task_id, name: str, args: dict, transport: httpx.BaseTransport | None = None) -> ClientResult:
    """app.agent.executors contract."""
    from app.skills.login import credential_for
    from app.vault import VaultError, secret_for_browser

    try:
        path, params = build_request(args)
        cred = credential_for(session, user, url="https://online.moysklad.ru/")
        if cred is None:
            raise MsError(
                "no_credential",
                "Нет доступа к МоемуСкладу в «Доступах». Читай данные с экрана, а пользователю скажи, что для "
                "проверки через API нужно сохранить логин и пароль МоегоСклада.",
            )
        try:
            secret = secret_for_browser(session, user, uuid.UUID(cred["id"]))
        except VaultError as exc:
            raise MsError(exc.code, "Ошибка: хранилище паролей недоступно.") from None
        data = fetch(path, params, auth_header(cred["username"], secret), transport)
        return ClientResult(format_result(data))
    except MsError as exc:
        return ClientResult(exc.text or f"Ошибка: {exc.code}", exc.code)
    except httpx.HTTPError as exc:
        log.info("moysklad api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: API МоегоСклада не ответил.", "api_unavailable")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(TOOL)
    register_executor(TOOL_NAMES, execute)
