"""Мок JSON API 1.2 МоегоСклада поверх состояния мира (state["moysklad"]).

Инструменты мира — те же moysklad_read / moysklad_write, что в проде (app.agent.moysklad): разбор
аргументов, выбор доступа, проверка учебного аккаунта и сверка после записи идут прод-кодом, а запросы
уходят в этот мок через httpx.MockTransport. Состояние простое и читаемое в проверках сценариев:
moysklad.products[article=GL-M-100].stock, moysklad.supplies[0].applicable.

Мок ведёт себя как настоящий API там, где это важно для агента: суммы в копейках, ссылки — meta.href,
проведённая приёмка добавляет остаток, нет обязательного поля — 400 с errors, чужой логин — 401.
PUT и DELETE мок не умеет: на них 405 и запись в self.forbidden (тесты проверяют, что их не было).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from app.agent.moysklad import API

ENTITY_OF = {"supply": "supplies", "customerorder": "customerorders", "invoiceout": "invoices"}


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _error(status: int, text: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"error": text}]})


class MoySkladMock:
    def __init__(self, state: dict) -> None:
        self.ms = state
        for key in ("organizations", "stores", "counterparties", "products", "supplies", "customerorders", "invoices"):
            self.ms.setdefault(key, [])
        self.ms.setdefault("next_id", 1000)
        self.requests: list[tuple[str, str]] = []  # (method, path) — что ушло в «API»
        self.forbidden: list[tuple[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- представление -------------------------------------------------------
    @staticmethod
    def meta(kind: str, row_id: str) -> dict:
        return {"meta": {"href": f"{API}entity/{kind}/{row_id}", "type": kind, "mediaType": "application/json"}}

    def ref(self, kind: str, items: list[dict], row_id: str) -> dict:
        row = next((r for r in items if r["id"] == row_id), None)
        return {**self.meta(kind, row_id), "name": row["name"]} if row else self.meta(kind, row_id)

    def product_out(self, p: dict) -> dict:
        out = {**self.meta("product", p["id"]), "id": p["id"], "name": p["name"]}
        for field in ("article", "code", "description"):
            if p.get(field):
                out[field] = p[field]
        if p.get("buy") is not None:
            out["buyPrice"] = {"value": round(p["buy"] * 100), "currency": self.meta("currency", "rub")}
        if p.get("sale") is not None:
            out["salePrices"] = [{"value": round(p["sale"] * 100), "priceType": {"name": "Цена продажи"}}]
        return out

    def doc_out(self, kind: str, doc: dict, expand_positions: bool) -> dict:
        out = {**self.meta(kind, doc["id"]), "id": doc["id"], "name": doc["name"], "applicable": doc["applicable"],
               "moment": "2026-11-02 12:00:00.000",
               "organization": self.ref("organization", self.ms["organizations"], doc["organization"]),
               "agent": self.ref("counterparty", self.ms["counterparties"], doc["agent"])}
        if doc.get("store"):
            out["store"] = self.ref("store", self.ms["stores"], doc["store"])
        if doc.get("customerorder"):
            out["customerOrder"] = self.meta("customerorder", doc["customerorder"])
        if doc.get("description"):
            out["description"] = doc["description"]
        rows = [{"quantity": p["quantity"], "price": round(p["price"] * 100),
                 "assortment": self.ref("product", self.ms["products"], p["product"])} for p in doc["positions"]]
        out["sum"] = sum(r["quantity"] * r["price"] for r in rows)
        if expand_positions:
            out["positions"] = {"meta": {"size": len(rows)}, "rows": rows}
        else:
            out["positions"] = {"meta": {"href": f"{API}entity/{kind}/{doc['id']}/positions", "size": len(rows)}}
        return out

    # --- запросы ---------------------------------------------------------------
    def account_login(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        for access in self.ms.get("access", []):
            login, secret = access["username"], access["secret"]
            if login.strip().lower() in {"token", "bearer"}:
                if header == f"Bearer {secret}":
                    return access.get("uid") or "admin@token-account"
            elif header == "Basic " + base64.b64encode(f"{login}:{secret}".encode()).decode():
                return access.get("uid") or login
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/api/remap/1.2/", 1)[-1].strip("/")
        self.requests.append((request.method, path))
        if request.url.host != "api.moysklad.ru":
            return _error(400, "не тот хост")
        if request.headers.get("accept-encoding", "") != "gzip":
            return _error(415, "нужен Accept-Encoding: gzip")
        uid = self.account_login(request)
        if uid is None:
            return _error(401, "Ошибка аутентификации")
        if request.method == "GET":
            return self.get(path, dict(request.url.params), uid)
        if request.method == "POST":
            return self.post(path, json.loads(request.content or b"{}"))
        self.forbidden.append((request.method, path))
        return _error(405, "метод не поддерживается моком")

    def _list(self, rows: list[dict]) -> httpx.Response:
        return httpx.Response(200, json={"meta": {"size": len(rows)}, "rows": rows})

    def _filtered(self, items: list[dict], params: dict, fields: tuple[str, ...]) -> list[dict]:
        found = list(items)
        for part in str(params.get("filter") or "").split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                found = [r for r in found if str(r.get(key) or "") == value]
        search = str(params.get("search") or "").casefold()
        if search:
            found = [r for r in found if any(search in str(r.get(f) or "").casefold() for f in fields)]
        return found

    def get(self, path: str, params: dict, uid: str) -> httpx.Response:
        expand = "positions" in str(params.get("expand") or "")
        if path == "context/employee":
            return httpx.Response(200, json={"id": _uuid(1), "name": "Администратор", "uid": uid})
        if path == "context/companysettings":
            return httpx.Response(200, json={
                "currency": self.meta("currency", "rub"),
                "priceTypes": [{**self.meta("pricetype", "sale"), "name": "Цена продажи"}],
            })
        if path == "report/stock/all":
            rows = [{**self.product_out(p), "stock": p.get("stock", 0), "minimumBalance": p.get("minimum", 0)}
                    for p in self._filtered(self.ms["products"], {"search": params.get("search")}, ("name", "article"))]
            if "underMinimum" in str(params.get("filter") or ""):
                rows = [r for r in rows if r["stock"] < r["minimumBalance"]]
            return self._list(rows)
        parts = path.split("/")
        if len(parts) < 2 or parts[0] != "entity":
            return _error(404, "нет такого ресурса")
        kind, row_id = parts[1], (parts[2] if len(parts) > 2 else "")
        simple = {"organization": "organizations", "store": "stores", "counterparty": "counterparties"}
        if kind in simple:
            items = self.ms[simple[kind]]
            if row_id:
                row = next((r for r in items if r["id"] == row_id), None)
                return httpx.Response(200, json={**self.meta(kind, row_id), **row}) if row else _error(404, "нет")
            return self._list([{**self.meta(kind, r["id"]), **r} for r in self._filtered(items, params, ("name", "inn"))])
        if kind == "product":
            if row_id:
                row = next((p for p in self.ms["products"] if p["id"] == row_id), None)
                return httpx.Response(200, json=self.product_out(row)) if row else _error(404, "нет")
            found = self._filtered(self.ms["products"], params, ("name", "article", "code"))
            return self._list([self.product_out(p) for p in found])
        if kind in ENTITY_OF:
            items = self.ms[ENTITY_OF[kind]]
            if row_id:
                doc = next((d for d in items if d["id"] == row_id), None)
                return httpx.Response(200, json=self.doc_out(kind, doc, expand)) if doc else _error(404, "нет")
            return self._list([self.doc_out(kind, d, expand) for d in self._filtered(items, params, ("name",))])
        return _error(404, "нет такого ресурса")

    # --- запись ------------------------------------------------------------------
    def next_id(self) -> str:
        self.ms["next_id"] += 1
        return _uuid(self.ms["next_id"])

    @staticmethod
    def _href_id(ref: Any) -> str:
        href = str(((ref or {}).get("meta") or {}).get("href") or "")
        return href.rstrip("/").rsplit("/", 1)[-1]

    def post(self, path: str, body: dict) -> httpx.Response:
        parts = path.split("/")
        if len(parts) != 2 or parts[0] != "entity" or parts[1] not in {"product", *ENTITY_OF}:
            self.forbidden.append(("POST", path))
            return _error(405, "мок умеет создавать только товар, приёмку, заказ и счёт")
        kind = parts[1]
        if kind == "product":
            if not body.get("name"):
                return _error(400, "Ошибка сохранения объекта: поле 'name' не может быть пустым")
            sale = (body.get("salePrices") or [{}])[0]
            if sale and not sale.get("priceType"):
                return _error(400, "Ошибка: у цены продажи нет типа цены")
            product = {"id": self.next_id(), "name": body["name"], "article": body.get("article", ""),
                       "code": body.get("code", ""), "description": body.get("description", ""), "stock": 0,
                       "buy": (body["buyPrice"]["value"] / 100) if body.get("buyPrice") else None,
                       "sale": (sale["value"] / 100) if sale.get("value") is not None else None}
            self.ms["products"].append(product)
            return httpx.Response(200, json=self.product_out(product))
        required = ("organization", "agent", "store") if kind == "supply" else ("organization", "agent")
        missing = [f for f in required if not body.get(f)]
        if missing:
            return _error(400, f"Ошибка сохранения объекта: поле '{missing[0]}' обязательно")
        positions = []
        for row in body.get("positions") or []:
            product_id = self._href_id(row.get("assortment"))
            if not any(p["id"] == product_id for p in self.ms["products"]):
                return _error(400, "Ошибка: позиция ссылается на несуществующий товар")
            positions.append({"product": product_id, "quantity": row.get("quantity", 1), "price": row.get("price", 0) / 100})
        items = self.ms[ENTITY_OF[kind]]
        doc = {"id": self.next_id(), "name": f"{len(items) + 1:05d}", "applicable": body.get("applicable", True),
               "organization": self._href_id(body["organization"]), "agent": self._href_id(body["agent"]),
               "store": self._href_id(body.get("store")) or None,
               "customerorder": self._href_id(body.get("customerOrder")) or None,
               "description": body.get("description", ""), "positions": positions}
        items.append(doc)
        if kind == "supply" and doc["applicable"]:
            for pos in positions:
                product = next(p for p in self.ms["products"] if p["id"] == pos["product"])
                product["stock"] = product.get("stock", 0) + pos["quantity"]
        return httpx.Response(200, json=self.doc_out(kind, doc, expand_positions=False))
