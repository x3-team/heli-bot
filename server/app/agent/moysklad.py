"""МойСклад JSON API 1.2 for the agent.

moysklad_read reads on any account: stock, products, counterparties, a saved document. It is for answers
that need exact numbers and for checking a document after «Сохранить».

moysklad_write creates a product, a supply (posted by default), a customer order and an invoice to a
buyer, and nothing else: only POST to those four collections, never PUT or DELETE, never another
entity (stores, counterparties, prices of existing products). It writes only to a training account:
the account (the part of the login after «@») must be in SANDBOX_ACCOUNTS or HELI_MOYSKLAD_SANDBOX_ACCOUNTS,
and the server checks the account behind the login with GET context/employee before the first POST.
A live account (a client's, Heli's own) is refused before any POST, with or without a «да»: the user
asked to keep live МойСклад accounts untouched, so there is no confirmation path here. After a POST
the record is read back by id and compared with what was asked.

Access: the user's МойСклад login in «Доступы» (site moysklad.ru / online.moysklad.ru). If the login
is «token», the password is used as a Bearer token. Requests go only to api.moysklad.ru.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import uuid
from collections.abc import Callable

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
    "Используй для точных чисел (остатки, неснижаемый остаток, цены) и чтобы проверить документ или карточку. "
    "Создать товар, приёмку, заказ покупателя или счёт — через moysklad_write, не через экран.",
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
_POSITIONS = {
    "type": "array",
    "description": "Позиции документа",
    "items": {
        "type": "object",
        "properties": {
            "product": {"type": "string", "description": "Товар: id, артикул или точное название"},
            "quantity": {"type": "number", "description": "Количество"},
            "price": {"type": "number", "description": "Цена за единицу в рублях; без неё — цена из карточки"},
        },
        "required": ["product", "quantity"],
    },
}
WRITE_TOOL = _fn(
    "moysklad_write",
    "Создать в МоёмСкладе через API: товар (create_product), приёмку с проведением (create_supply), заказ "
    "покупателя (create_customerorder), счёт покупателю (create_invoiceout, по заказу — customerorder_id). "
    "Это первый путь для таких задач: форму на экране не открывай. Пишет только в учебный аккаунт из "
    "«Доступов»; боевой аккаунт сервер отклонит — тогда честно скажи пользователю, что через API записать "
    "нельзя, и не изображай успех. Удалять и менять чужие записи нельзя. Ответ — id и поля записи, уже "
    "проверенные повторным чтением.",
    {
        "action": {
            "type": "string",
            "enum": ["create_product", "create_supply", "create_customerorder", "create_invoiceout"],
            "description": "Что создать",
        },
        "account": {"type": "string", "description": "Учебный аккаунт (часть логина после @), если их несколько"},
        "name": {"type": "string", "description": "Товар: название"},
        "article": {"type": "string", "description": "Товар: артикул"},
        "code": {"type": "string", "description": "Товар: код"},
        "buy_price": {"type": "number", "description": "Товар: закупочная цена, ₽"},
        "sale_price": {"type": "number", "description": "Товар: цена продажи, ₽"},
        "agent": {"type": "string", "description": "Документ: контрагент — id, ИНН или точное название"},
        "organization": {"type": "string", "description": "Документ: своё юрлицо (если одно — не нужно)"},
        "store": {"type": "string", "description": "Приёмка, заказ: склад (если один — не нужен)"},
        "positions": _POSITIONS,
        "customerorder_id": {"type": "string", "description": "Счёт: id заказа покупателя, по которому он выставлен"},
        "applicable": {"type": "boolean", "description": "Провести документ. Приёмка и заказ — по умолчанию да"},
        "description": {"type": "string", "description": "Комментарий"},
    },
    ["action"],
)
TOOL_NAMES = ("moysklad_read", "moysklad_write")
# System prompt line when moysklad_write is offered: the browser hint alone sends the model to the form.
WRITE_HINT = (
    " МойСклад: товар, приёмку, заказ покупателя и счёт создавай через moysklad_write (API), а не в форме на "
    "экране. Если moysklad_write отказал, ничего не создано: честно скажи, что через API записать нельзя и "
    "почему, и не пиши об успехе после кликов по форме."
)

# action -> collection it creates in. Only POST to these, never with an id (no updates, no deletes).
WRITE_ACTIONS = {
    "create_product": "entity/product",
    "create_supply": "entity/supply",
    "create_customerorder": "entity/customerorder",
    "create_invoiceout": "entity/invoiceout",
}
WRITE_PATHS = frozenset(WRITE_ACTIONS.values())
KIND_NAMES = {
    "entity/product": "товар",
    "entity/supply": "приёмка",
    "entity/customerorder": "заказ покупателя",
    "entity/invoiceout": "счёт покупателю",
}
# Training accounts (the part of a МойСклад login after «@»). Everything else is live: read only.
SANDBOX_ACCOUNTS: tuple[str, ...] = ()
SANDBOX_ENV = "HELI_MOYSKLAD_SANDBOX_ACCOUNTS"
SITE_SUFFIX = "moysklad.ru"
MAX_POSITIONS = 50


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
        out["salePrices"] = [
            round(p["value"] / 100, 2) for p in row["salePrices"][:3] if isinstance(p, dict) and isinstance(p.get("value"), (int, float))
        ]
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


def _headers(authorization: str) -> dict:
    return {"Authorization": authorization, "Accept-Encoding": "gzip", "Accept": "application/json;charset=utf-8"}


def _checked(response: httpx.Response) -> dict:
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


def fetch(path: str, params: dict, authorization: str, transport: httpx.BaseTransport | None = None) -> dict:
    with httpx.Client(base_url=API, timeout=TIMEOUT_S, transport=transport, follow_redirects=False) as client:
        response = client.get(path, params=params, headers=_headers(authorization))
    return _checked(response)


# --- writing -----------------------------------------------------------------------------------------


def account_of(login: str | None) -> str:
    """The МойСклад account in a login: admin@romashka → romashka. "" for a token or a bare name."""
    text = str(login or "").strip().lower()
    return text.rsplit("@", 1)[1].strip() if "@" in text else ""


def requested_account(value) -> str:
    """The account the model named: admin@acc / acc → acc; online.moysklad.ru means «not named»."""
    text = str(value or "").strip().lower()
    if not text or is_moysklad_site(text):
        return ""
    return account_of(text) or text


def sandbox_accounts() -> set[str]:
    extra = os.environ.get(SANDBOX_ENV, "")
    return {account_of(a) or a.strip().lower() for a in (*SANDBOX_ACCOUNTS, *extra.split(",")) if a.strip()}


def is_moysklad_site(site: str | None) -> bool:
    from app.skills.hosts import host_matches, host_of

    return host_matches(host_of(site), SITE_SUFFIX)


def _rub(value, field: str) -> int | None:
    """Rubles from the model to kopecks for the API."""
    if value in (None, ""):
        return None
    try:
        amount = float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        raise MsError("bad_args", f"Ошибка: {field} — число в рублях") from None
    if amount < 0 or amount > 1e10:
        raise MsError("bad_args", f"Ошибка: {field} вне допустимых значений")
    return int(round(amount * 100))


def _meta(row: dict) -> dict:
    return {"meta": row["meta"]}


def _id_of(row) -> str:
    """id of a row or of a {"meta": {"href": …/id}} reference."""
    if not isinstance(row, dict):
        return ""
    if row.get("id"):
        return str(row["id"])
    href = str((row.get("meta") or {}).get("href") or "")
    return href.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0] if href else ""


class Api:
    """GET anywhere under API; POST only to WRITE_PATHS. No PUT, no DELETE, no id in a POST path."""

    def __init__(self, authorization: str, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=API, timeout=TIMEOUT_S, transport=transport, follow_redirects=False, headers=_headers(authorization)
        )

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def get(self, path: str, params: dict | None = None) -> dict:
        return _checked(self._client.get(path, params=params or {}))

    def post(self, path: str, body: dict) -> dict:
        if path not in WRITE_PATHS:
            raise MsError("write_forbidden", "Ошибка: через API можно только создать товар, приёмку, заказ или счёт.")
        return _checked(self._client.post(path, json=body, headers={"Content-Type": "application/json"}))


def _rows(data: dict) -> list[dict]:
    return [row for row in (data.get("rows") or []) if isinstance(row, dict)]


def _pick(rows: list[dict], query: str, what: str, *fields: str) -> dict:
    """One row for the query: the exact match on a field, else the only row. Several — ask which."""
    wanted = query.strip().casefold()
    exact = [r for r in rows if any(str(r.get(f) or "").strip().casefold() == wanted for f in fields)]
    if len(exact) == 1:
        return exact[0]
    if len(rows) == 1 and not exact:
        return rows[0]
    if not rows:
        raise MsError("not_found", f"Ошибка: {what} «{query}» в МоёмСкладе не найден. Уточни у пользователя или проверь через moysklad_read.")
    names = ", ".join(f"{r.get('name')} (id {r.get('id')})" for r in (exact or rows)[:10])
    raise MsError("ambiguous", f"Ошибка: {what} «{query}» — несколько вариантов: {names}. Укажи id.")


def _find(api: Api, entity: str, query, what: str) -> dict:
    """A reference (counterparty, product, store, organization) by id, ИНН, article or name. Reads only."""
    query = str(query or "").strip()
    if not query:
        raise MsError("bad_args", f"Ошибка: укажи {what}")
    if _ID.match(query):
        return api.get(f"entity/{entity}/{query.lower()}")
    if entity == "counterparty" and re.fullmatch(r"\d{10}|\d{12}", query):
        rows = _rows(api.get("entity/counterparty", {"filter": f"inn={query}", "limit": 10}))
        if rows:
            return _pick(rows, query, what, "inn")
    if entity == "product":
        rows = _rows(api.get("entity/product", {"filter": f"article={query}", "limit": 10}))
        if rows:
            return _pick(rows, query, what, "article")
    rows = _rows(api.get(f"entity/{entity}", {"search": query[:200], "limit": 10}))
    return _pick(rows, query, what, "name", "article", "code", "inn")


def _only(api: Api, entity: str, query, what: str) -> dict:
    """The named one, or the only one in the account (most have one organization and one store)."""
    if str(query or "").strip():
        return _find(api, entity, query, what)
    rows = _rows(api.get(f"entity/{entity}", {"limit": 10}))
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise MsError("not_found", f"Ошибка: в МоёмСкладе нет ни одного объекта «{what}».")
    names = ", ".join(str(r.get("name")) for r in rows[:10])
    raise MsError("ambiguous", f"Ошибка: укажи {what}: {names}")


def _sale_value(product: dict) -> int | None:
    for price in product.get("salePrices") or []:
        if isinstance(price, dict) and isinstance(price.get("value"), (int, float)) and price["value"] > 0:
            return int(price["value"])
    return None


def _buy_value(product: dict) -> int | None:
    value = (product.get("buyPrice") or {}).get("value")
    return int(value) if isinstance(value, (int, float)) else None


def _positions_body(api: Api, positions, default_price: Callable[[dict], int | None]) -> list[dict]:
    if not isinstance(positions, list) or not positions:
        raise MsError("bad_args", "Ошибка: укажи positions: товар и количество")
    if len(positions) > MAX_POSITIONS:
        raise MsError("bad_args", f"Ошибка: не больше {MAX_POSITIONS} позиций за раз")
    body = []
    for item in positions:
        if not isinstance(item, dict):
            raise MsError("bad_args", "Ошибка: позиция — {product, quantity, price}")
        product = _find(api, "product", item.get("product"), "товар")
        try:
            quantity = float(item.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            raise MsError("bad_args", f"Ошибка: количество для «{product.get('name')}» должно быть больше нуля")
        price = _rub(item.get("price"), "price")
        if price is None:
            price = default_price(product) or 0
        body.append({"quantity": quantity, "price": price, "assortment": _meta(product)})
    return body


def _product_body(api: Api, args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        raise MsError("bad_args", "Ошибка: укажи name — название товара")
    body: dict = {"name": name[:255]}
    for field in ("article", "code", "description"):
        value = str(args.get(field) or "").strip()
        if value:
            body[field] = value[:255] if field != "description" else value[:4000]
    buy, sale = _rub(args.get("buy_price"), "buy_price"), _rub(args.get("sale_price"), "sale_price")
    if buy is not None or sale is not None:
        settings = api.get("context/companysettings")
        currency = settings.get("currency") if isinstance(settings.get("currency"), dict) else None
        if buy is not None:
            body["buyPrice"] = {"value": buy, **({"currency": _meta(currency)} if currency else {})}
        if sale is not None:
            types = [t for t in settings.get("priceTypes") or [] if isinstance(t, dict) and t.get("meta")]
            if not types:
                raise MsError("api_error", "Ошибка: в МоёмСкладе нет типа цены продажи — цену продажи не задать.")
            body["salePrices"] = [{"value": sale, "priceType": _meta(types[0]), **({"currency": _meta(currency)} if currency else {})}]
    return body


def _document_body(api: Api, path: str, args: dict) -> dict:
    order = None
    if path == "entity/invoiceout" and str(args.get("customerorder_id") or "").strip():
        order_id = str(args["customerorder_id"]).strip()
        if not _ID.match(order_id):
            raise MsError("bad_id", "Ошибка: customerorder_id — это uuid заказа из ответа moysklad_write")
        order = api.get(f"entity/customerorder/{order_id.lower()}", {"expand": "positions,agent,organization"})
    body: dict = {}
    if args.get("organization") or order is None:
        body["organization"] = _meta(_only(api, "organization", args.get("organization"), "организация"))
    else:
        body["organization"] = {"meta": order["organization"]["meta"]}
    if args.get("agent") or order is None:
        body["agent"] = _meta(_find(api, "counterparty", args.get("agent"), "контрагент"))
    else:
        body["agent"] = {"meta": order["agent"]["meta"]}
    if path in ("entity/supply", "entity/customerorder"):
        if path == "entity/supply" or args.get("store"):
            body["store"] = _meta(_only(api, "store", args.get("store"), "склад"))
    if order is not None:
        body["customerOrder"] = {"meta": order["meta"]}
    if args.get("positions") or order is None:
        default = _buy_value if path == "entity/supply" else _sale_value
        body["positions"] = _positions_body(api, args.get("positions"), default)
    else:
        rows = (order.get("positions") or {}).get("rows") or []
        body["positions"] = [
            {"quantity": r["quantity"], "price": r.get("price", 0), "assortment": {"meta": r["assortment"]["meta"]}}
            for r in rows[:MAX_POSITIONS]
        ]
        if not body["positions"]:
            raise MsError("bad_args", "Ошибка: в заказе нет позиций — укажи positions для счёта")
    if path in ("entity/supply", "entity/customerorder"):
        body["applicable"] = args.get("applicable") is not False  # приёмка проводится, если не сказано иначе
    elif isinstance(args.get("applicable"), bool):
        body["applicable"] = args["applicable"]
    description = str(args.get("description") or "").strip()
    if description:
        body["description"] = description[:4000]
    return body


def _position_key(row: dict) -> tuple[str, float, int]:
    return (_id_of(row.get("assortment")), float(row.get("quantity") or 0), int(row.get("price") or 0))


def _mismatches(path: str, body: dict, saved: dict) -> list[str]:
    """What the second GET shows differently from what was sent."""
    problems = []
    for field in ("name", "article", "code"):
        if field in body and str(saved.get(field) or "") != str(body[field]):
            problems.append(f"{field}: ждали «{body[field]}», в МоёмСкладе «{saved.get(field)}»")
    if "buyPrice" in body and _buy_value(saved) != body["buyPrice"]["value"]:
        problems.append("закупочная цена не совпала")
    if "salePrices" in body and _sale_value(saved) != body["salePrices"][0]["value"]:
        problems.append("цена продажи не совпала")
    for ref in ("agent", "organization", "store", "customerOrder"):
        if ref in body and _id_of(saved.get(ref)) != _id_of(body[ref]):
            problems.append(f"{ref} не совпал")
    if "applicable" in body and saved.get("applicable") is not body["applicable"]:
        problems.append("документ не проведён" if body["applicable"] else "документ проведён, хотя не просили")
    if "positions" in body:
        rows = (saved.get("positions") or {}).get("rows") if isinstance(saved.get("positions"), dict) else None
        if not isinstance(rows, list) or sorted(map(_position_key, rows)) != sorted(map(_position_key, body["positions"])):
            problems.append("позиции не совпали")
    return problems


def _written(path: str, account: str, saved: dict, problems: list[str]) -> ClientResult:
    kind = KIND_NAMES[path]
    data = compact(saved)
    text = f"Создано в учебном аккаунте МоегоСклада {account}: {kind}, id {saved.get('id')}.\n" + json.dumps(data, ensure_ascii=False)
    if problems:
        text += "\nПроверка повторным чтением: НЕ совпало — " + "; ".join(problems) + ". Не говори пользователю, что всё сделано."
        return ClientResult(text[: RESULT_CHARS + 300], "verify_mismatch")
    return ClientResult(text[:RESULT_CHARS] + "\nПроверено повторным чтением по id: поля совпадают с запросом.")


def choose_credential(creds: list[dict], account: str, sandbox: set[str]) -> dict:
    """The «Доступы» entry to write with. Refuses an account not in «Доступах» and a live account."""
    ms = [c for c in creds if is_moysklad_site(c.get("site")) and str(c.get("username") or "").strip()]
    if not ms:
        raise MsError(
            "no_credential",
            "Не выполнено: в «Доступах» нет логина МоегоСклада, через API записать нельзя. Ничего не создано. "
            "Скажи пользователю, что нужен логин учебного аккаунта МоегоСклада с доступом к API.",
        )
    wanted = requested_account(account)
    if wanted:
        ms = [c for c in ms if account_of(c["username"]) == wanted]
        if not ms:
            raise MsError(
                "not_in_access",
                f"Не выполнено: аккаунта МоегоСклада «{wanted}» нет в «Доступах» — записывать туда нельзя. Ничего не создано.",
            )
    training = [c for c in ms if account_of(c["username"]) in sandbox]
    if training:
        return training[0]
    tokens = [c for c in ms if not account_of(c["username"])]
    if tokens and sandbox:
        return tokens[0]  # account unknown from a token login: context/employee decides below
    live = ", ".join(sorted({account_of(c["username"]) or "по токену" for c in ms}))
    raise MsError("write_forbidden", _live_text(live))


def _live_text(account: str) -> str:
    return (
        f"Не выполнено: аккаунт МоегоСклада {account} боевой, через API туда не пишем (записывать можно только в "
        "учебный аккаунт). Ничего не создано. Не создавай это на экране молча: честно скажи пользователю, что "
        "через API записать нельзя, и что нужно, чтобы записать."
    )


def run_write(
    args: dict,
    creds: list[dict],
    secret_of: Callable[[dict], str],
    transport: httpx.BaseTransport | None = None,
    sandbox: set[str] | None = None,
) -> ClientResult:
    """moysklad_write over given «Доступы» rows (id, site, username) — the vault or a test world."""
    action = str(args.get("action") or "")
    path = WRITE_ACTIONS.get(action)
    try:
        if path is None:
            raise MsError("bad_action", "Ошибка: action — одно из " + ", ".join(WRITE_ACTIONS))
        allowed = sandbox_accounts() if sandbox is None else {a.lower() for a in sandbox}
        cred = choose_credential(creds, str(args.get("account") or ""), allowed)
        with Api(auth_header(cred["username"], secret_of(cred)), transport) as api:
            # Second lock: the account the API itself reports for this login, not what the login says.
            uid = str(api.get("context/employee").get("uid") or "")
            account = account_of(uid)
            said = account_of(cred["username"])
            if account not in allowed or (said and said != account):
                raise MsError("write_forbidden", _live_text(account or said or "по токену"))
            body = _product_body(api, args) if path == "entity/product" else _document_body(api, path, args)
            if path == "entity/product" and body.get("article"):
                same = _rows(api.get("entity/product", {"filter": f"article={body['article']}", "limit": 5}))
                if same:
                    return ClientResult(
                        f"Не создано: товар с артикулом {body['article']} уже есть (id {same[0].get('id')}). Второй не "
                        "заводим.\n" + json.dumps(compact(same[0]), ensure_ascii=False),
                        "already_exists",
                    )
            created = api.post(path, body)
            new_id = str(created.get("id") or "")
            if not _ID.match(new_id):
                raise MsError("api_error", "Ошибка: МойСклад не вернул id новой записи — проверь через moysklad_read.")
            params = {"expand": "positions"} if path != "entity/product" else {}
            saved = api.get(f"{path}/{new_id.lower()}", params)
            log.info("moysklad sandbox write %s account=%s", action, account)
            return _written(path, account, saved, _mismatches(path, body, saved))
    except MsError as exc:
        if exc.code in ("write_forbidden", "not_in_access", "no_credential"):
            log.info("moysklad write refused: %s", exc.code)
        return ClientResult(exc.text or f"Ошибка: {exc.code}", exc.code)
    except httpx.HTTPError as exc:
        log.info("moysklad api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: API МоегоСклада не ответил. Не знаю, создана ли запись — проверь через moysklad_read.", "api_unavailable")


def run_read(args: dict, cred: dict | None, secret_of: Callable[[dict], str], transport: httpx.BaseTransport | None = None) -> ClientResult:
    try:
        path, params = build_request(args)
        if cred is None:
            raise MsError(
                "no_credential",
                "Нет доступа к МоемуСкладу в «Доступах». Читай данные с экрана, а пользователю скажи, что для "
                "проверки через API нужно сохранить логин и пароль МоегоСклада.",
            )
        data = fetch(path, params, auth_header(cred["username"], secret_of(cred)), transport)
        return ClientResult(format_result(data))
    except MsError as exc:
        return ClientResult(exc.text or f"Ошибка: {exc.code}", exc.code)
    except httpx.HTTPError as exc:
        log.info("moysklad api failed: %s", type(exc).__name__)
        return ClientResult("Ошибка: API МоегоСклада не ответил.", "api_unavailable")


def execute(session, user, task_id, name: str, args: dict, transport: httpx.BaseTransport | None = None) -> ClientResult:
    """app.agent.executors contract."""
    from app.skills.login import credential_for
    from app.vault import VaultError, list_credentials, secret_for_browser

    def secret_of(cred: dict) -> str:
        try:
            return secret_for_browser(session, user, uuid.UUID(str(cred["id"])))
        except VaultError as exc:
            raise MsError(exc.code, "Ошибка: хранилище паролей недоступно.") from None

    if name == "moysklad_write":
        try:
            creds = list_credentials(session, user)
        except VaultError as exc:
            return ClientResult("Ошибка: хранилище паролей недоступно.", exc.code)
        return run_write(args, creds, secret_of, transport)
    return run_read(args, credential_for(session, user, url="https://online.moysklad.ru/"), secret_of, transport)


def classify(args: dict):
    """write_policy rule (PR #61). moysklad_write has no live path: a training account writes at once, and a
    live one is refused by the executor before any POST, «да» or not. So the loop never shows a card for it."""
    account = requested_account(args.get("account"))
    what = f"{KIND_NAMES.get(WRITE_ACTIONS.get(str(args.get('action') or ''), ''), 'запись')} в МоёмСкладе"
    if not account or account in sandbox_accounts():
        return _write_policy.sandbox(f"МойСклад {account or 'учебный аккаунт'}", what, "create")
    return _write_policy.read(f"МойСклад {account}")  # refused by the executor: nothing is written


try:  # the shared write policy lands with PR #61; until then the guard's name rules apply (no question)
    from app.agent import write_policy as _write_policy
except ImportError:  # pragma: no cover - main before #61
    _write_policy = None
else:
    _write_policy.register("moysklad_write", classify)


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(TOOL)
    register_client_tool(WRITE_TOOL)
    register_executor(TOOL_NAMES, execute)
