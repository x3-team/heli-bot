"""Мок REST API v4 amoCRM поверх состояния мира (state["amocrm"]).

Инструменты мира — те же amocrm_read / amocrm_write, что в проде (app.agent.amocrm): проверка путей,
методов, тела и учебного/боевого аккаунта идёт прод-кодом, а запрос уходит в этот мок через
httpx.MockTransport. Состояние хранится в простом виде (contacts, deals со stage по названию), чтобы
проверки сценариев читались: amocrm.deals[id=4001].stage.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

import httpx

PIPELINE_ID = 7001
UNSORTED_STATUS = 70000
WON, LOST = 142, 143
FINAL = {"Успешно реализовано": WON, "Закрыто и не реализовано": LOST}


def _response(status: int, data: Any = None) -> httpx.Response:
    if data is None:
        return httpx.Response(status)
    return httpx.Response(status, json=data)


def _problem(status: int, title: str, detail: str = "", errors: Any = None) -> httpx.Response:
    body: dict = {"title": title, "status": status, "detail": detail}
    if errors is not None:
        body["validation-errors"] = errors
    return _response(status, body)


class AmoMock:
    def __init__(self, state: dict, token: str) -> None:
        self.amo = state
        self.token = token
        self.amo.setdefault("tasks", [])
        self.amo.setdefault("unsorted", [])
        self.amo.setdefault("users", [{"id": 1001, "name": self.amo.get("me", "Я")}])
        if "companies" not in self.amo:
            names = []
            for contact in self.amo["contacts"]:
                if contact.get("company") and contact["company"] not in names:
                    names.append(contact["company"])
            self.amo["companies"] = [{"id": 601 + i, "name": n} for i, n in enumerate(names)]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- справочники ---------------------------------------------------------
    def status_id(self, stage: str) -> int:
        if stage in FINAL:
            return FINAL[stage]
        return 70001 + self.amo["stages"].index(stage)

    def stage_of(self, status_id: Any) -> str | None:
        try:
            wanted = int(status_id)
        except (TypeError, ValueError):
            return None
        for stage in self.amo["stages"]:
            if self.status_id(stage) == wanted:
                return stage
        return None

    def user_id(self, name: str | None) -> int | None:
        for user in self.amo["users"]:
            if user["name"] == name:
                return user["id"]
        return None

    def user_name(self, user_id: Any) -> str | None:
        for user in self.amo["users"]:
            if str(user["id"]) == str(user_id):
                return user["name"]
        return None

    def company(self, name: str | None) -> dict | None:
        return next((c for c in self.amo["companies"] if c["name"] == name), None)

    def next_id(self) -> int:
        value = self.amo["next_id"]
        self.amo["next_id"] += 1
        return value

    # --- представление ---------------------------------------------------------
    def lead_out(self, deal: dict, with_contacts: bool) -> dict:
        out = {
            "id": deal["id"], "name": deal["name"], "price": deal["price"],
            "status_id": self.status_id(deal["stage"]), "pipeline_id": PIPELINE_ID,
            "responsible_user_id": self.user_id(deal.get("responsible")),
            "_links": {"self": {"href": f"/api/v4/leads/{deal['id']}"}},
        }
        if with_contacts and deal.get("contact_id"):
            out["_embedded"] = {"contacts": [{"id": deal["contact_id"], "is_main": True}]}
        return out

    def contact_out(self, contact: dict) -> dict:
        fields = []
        if contact.get("phone"):
            fields.append({"field_code": "PHONE", "field_name": "Телефон", "values": [{"value": contact["phone"]}]})
        if contact.get("email"):
            fields.append({"field_code": "EMAIL", "field_name": "Email", "values": [{"value": contact["email"]}]})
        out = {"id": contact["id"], "name": contact["name"], "custom_fields_values": fields or None}
        company = self.company(contact.get("company"))
        if company:
            out["_embedded"] = {"companies": [{"id": company["id"], "name": company["name"]}]}
        return out

    def task_out(self, task: dict) -> dict:
        return {
            "id": task["id"], "text": task["text"], "entity_id": task.get("entity_id"),
            "entity_type": task.get("entity_type"), "complete_till": task["complete_till"],
            "responsible_user_id": self.user_id(task["responsible"]), "is_completed": task.get("is_completed", False),
        }

    def unsorted_out(self, item: dict) -> dict:
        return {
            "uid": item["uid"], "source_name": item["source_name"], "category": item["category"],
            "pipeline_id": PIPELINE_ID, "created_at": item["created_at"],
            "metadata": {"form_name": item.get("form_name"), "text": item.get("text")},
            "_embedded": {
                "leads": [{"name": item["lead"]["name"], "price": item["lead"].get("price", 0)}],
                "contacts": [self.contact_out({"id": 0, **item["contact"]}) | {"id": None}],
            },
        }

    # --- запрос ---------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return _problem(401, "Unauthorized")
        path = request.url.path.removeprefix("/api/v4/").strip("/")
        query = dict(parse_qsl(request.url.query.decode()))
        body = json.loads(request.content) if request.content else None
        if request.method == "GET":
            return self.get(path, query)
        return self.write(request.method, path, body)

    def get(self, path: str, query: dict) -> httpx.Response:
        from evals.world import _digits, _match

        amo = self.amo
        words = query.get("query", "")
        if path == "account":
            return _response(200, {"id": 31337, "name": "Учебный аккаунт Heli", "subdomain": amo["subdomain"],
                                   "current_user_id": self.user_id(amo["me"]), "currency": "RUB"})
        if path == "leads/pipelines":
            statuses = [{"id": UNSORTED_STATUS, "name": "Неразобранное", "type": 1}]
            statuses += [{"id": self.status_id(s), "name": s, "type": 0} for s in amo["stages"]]
            return _response(200, {"_embedded": {"pipelines": [
                {"id": PIPELINE_ID, "name": "Воронка", "is_main": True, "_embedded": {"statuses": statuses}}]}})
        if path == "users":
            return _response(200, {"_embedded": {"users": [{"id": u["id"], "name": u["name"]} for u in amo["users"]]}})
        if path == "leads":
            names = {c["id"]: c["name"] for c in amo["contacts"]}
            deals = [d for d in amo["deals"]
                     if not words or _match(words, d["name"], d["stage"], names.get(d.get("contact_id")))]
            wanted = {v for k, v in query.items() if re.fullmatch(r"filter\[statuses\]\[\d+\]\[status_id\]", k)}
            if wanted:
                deals = [d for d in deals if str(self.status_id(d["stage"])) in wanted]
            ids = {v for k, v in query.items() if re.fullmatch(r"filter\[id\](\[\d+\])?", k)}
            if ids:
                deals = [d for d in deals if str(d["id"]) in ids]
            if not deals:
                return _response(204)
            with_contacts = "contacts" in query.get("with", "")
            return _response(200, {"_page": 1, "_embedded": {"leads": [self.lead_out(d, with_contacts) for d in deals]}})
        match = re.fullmatch(r"leads/(\d+)", path)
        if match:
            deal = next((d for d in amo["deals"] if str(d["id"]) == match.group(1)), None)
            return _response(200, self.lead_out(deal, True)) if deal else _problem(404, "Not Found")
        match = re.fullmatch(r"leads/(\d+)/notes", path)
        if match:
            deal = next((d for d in amo["deals"] if str(d["id"]) == match.group(1)), None)
            if deal is None:
                return _problem(404, "Not Found")
            notes = [{"id": i + 1, "entity_id": deal["id"], "note_type": "common", "params": {"text": t}}
                     for i, t in enumerate(deal["notes"])]
            return _response(200, {"_embedded": {"notes": notes}}) if notes else _response(204)
        if path == "contacts":
            phone = _digits(words)
            found = [c for c in amo["contacts"]
                     if not words or _match(words, c["name"], c.get("company"), c.get("email"))
                     or (len(phone) >= 6 and phone[-10:] in _digits(c.get("phone", "")))]
            if not found:
                return _response(204)
            return _response(200, {"_page": 1, "_embedded": {"contacts": [self.contact_out(c) for c in found]}})
        match = re.fullmatch(r"contacts/(\d+)", path)
        if match:
            contact = next((c for c in amo["contacts"] if str(c["id"]) == match.group(1)), None)
            return _response(200, self.contact_out(contact)) if contact else _problem(404, "Not Found")
        if path == "companies":
            found = [c for c in amo["companies"] if not words or _match(words, c["name"])]
            return _response(200, {"_embedded": {"companies": found}}) if found else _response(204)
        if path == "tasks":
            tasks = [self.task_out(t) for t in amo["tasks"]]
            return _response(200, {"_embedded": {"tasks": tasks}}) if tasks else _response(204)
        match = re.fullmatch(r"tasks/(\d+)", path)
        if match:
            task = next((t for t in amo["tasks"] if str(t["id"]) == match.group(1)), None)
            return _response(200, self.task_out(task)) if task else _problem(404, "Not Found")
        if path == "leads/unsorted":
            items = [self.unsorted_out(u) for u in amo["unsorted"]]
            return _response(200, {"_page": 1, "_embedded": {"unsorted": items}}) if items else _response(204)
        return _problem(404, "Not Found", "нет в мок-мире")

    def write(self, method: str, path: str, body: Any) -> httpx.Response:
        items = body if isinstance(body, list) else [body]
        if path == "contacts" and method == "POST":
            made = [self.add_contact(item) for item in items]
            return _response(200, {"_embedded": {"contacts": [{"id": c["id"], "request_id": str(i)} for i, c in enumerate(made)]}})
        if path == "companies" and method == "POST":
            made = []
            for item in items:
                company = {"id": self.next_id(), "name": str(item.get("name") or "")}
                self.amo["companies"].append(company)
                made.append(company)
            return _response(200, {"_embedded": {"companies": [{"id": c["id"]} for c in made]}})
        if path == "leads" and method == "POST":
            made = []
            for item in items:
                deal = self.add_lead(item)
                if isinstance(deal, httpx.Response):
                    return deal
                made.append(deal)
            return _response(200, {"_embedded": {"leads": [{"id": d["id"], "request_id": str(i)} for i, d in enumerate(made)]}})
        if path == "leads/complex" and method == "POST":
            out = []
            for item in items:
                contacts = (item.get("_embedded") or {}).get("contacts") or []
                contact_ids = [self.add_contact(c)["id"] if "id" not in c else c["id"] for c in contacts]
                lead = dict(item)
                lead["_embedded"] = {"contacts": [{"id": cid} for cid in contact_ids]}
                deal = self.add_lead(lead)
                if isinstance(deal, httpx.Response):
                    return deal
                out.append({"id": deal["id"], "contact_id": contact_ids[0] if contact_ids else None})
            return _response(200, out)
        match = re.fullmatch(r"leads/(\d+)", path)
        if match and method == "PATCH":
            return self.patch_lead(match.group(1), body)
        if path == "leads" and method == "PATCH":
            for item in items:
                problem = self.patch_lead(str(item.get("id")), item)
                if problem.status_code != 200:
                    return problem
            return _response(200, {"_embedded": {"leads": [{"id": item["id"]} for item in items]}})
        match = re.fullmatch(r"leads/(?:(\d+)/)?notes", path)
        if match and method == "POST":
            for item in items:
                deal_id = match.group(1) or str(item.get("entity_id"))
                deal = next((d for d in self.amo["deals"] if str(d["id"]) == deal_id), None)
                text = (item.get("params") or {}).get("text")
                if deal is None:
                    return _problem(404, "Not Found")
                if item.get("note_type") != "common" or not text:
                    return _problem(400, "Bad Request", errors=[{"path": "params.text", "detail": "note_type common и params.text обязательны"}])
                deal["notes"].append(str(text))
            return _response(200, {"_embedded": {"notes": [{"id": i + 1} for i in range(len(items))]}})
        if path == "tasks" and method == "POST":
            made = []
            for item in items:
                if not item.get("text") or not isinstance(item.get("complete_till"), int):
                    return _problem(400, "Bad Request", errors=[{"path": "complete_till", "detail": "text и complete_till (unix-время, число) обязательны"}])
                responsible = self.user_name(item.get("responsible_user_id")) or self.amo["me"]
                task = {"id": self.next_id(), "text": str(item["text"]), "entity_id": item.get("entity_id"),
                        "entity_type": item.get("entity_type"), "complete_till": item["complete_till"],
                        "due": datetime.fromtimestamp(item["complete_till"], timezone.utc).strftime("%Y-%m-%d"),
                        "responsible": responsible, "is_completed": False}
                self.amo["tasks"].append(task)
                made.append(task)
            return _response(200, {"_embedded": {"tasks": [{"id": t["id"]} for t in made]}})
        match = re.fullmatch(r"leads/unsorted/([^/]+)/accept", path)
        if match and method == "POST":
            item = next((u for u in self.amo["unsorted"] if u["uid"] == match.group(1)), None)
            if item is None:
                return _problem(404, "Not Found")
            stage = self.stage_of((body or {}).get("status_id")) if (body or {}).get("status_id") else self.amo["stages"][0]
            if stage is None:
                return _problem(400, "Bad Request", errors=[{"path": "status_id", "detail": "нет такого этапа"}])
            contact = self.add_contact({"name": item["contact"]["name"]}, item["contact"])
            deal = {"id": self.next_id(), "name": item["lead"]["name"], "price": float(item["lead"].get("price", 0)),
                    "contact_id": contact["id"], "stage": stage, "responsible": self.amo["me"], "notes": []}
            self.amo["deals"].append(deal)
            self.amo["unsorted"].remove(item)
            return _response(200, {"uid": item["uid"], "_embedded": {"leads": [{"id": deal["id"]}], "contacts": [{"id": contact["id"]}]}})
        return _problem(404, "Not Found", "нет в мок-мире")

    def add_contact(self, item: dict, plain: dict | None = None) -> dict:
        plain = dict(plain or {})
        contact_id = self.next_id()  # как в amoCRM: id контакта раньше id новой компании из того же запроса
        name = str(item.get("name") or " ".join(str(item.get(k) or "") for k in ("first_name", "last_name")).strip())
        for field in item.get("custom_fields_values") or []:
            value = ((field.get("values") or [{}])[0]).get("value")
            if field.get("field_code") == "PHONE":
                plain["phone"] = str(value)
            if field.get("field_code") == "EMAIL":
                plain["email"] = str(value)
        for company in (item.get("_embedded") or {}).get("companies") or []:
            known = next((c for c in self.amo["companies"] if str(c["id"]) == str(company.get("id"))), None)
            plain["company"] = known["name"] if known else str(company.get("name") or "")
            if not known and plain["company"]:
                self.amo["companies"].append({"id": self.next_id(), "name": plain["company"]})
        contact = {"id": contact_id, "name": name or plain.get("name", ""), "phone": plain.get("phone", ""),
                   "email": plain.get("email", ""), "company": plain.get("company", "")}
        self.amo["contacts"].append(contact)
        return contact

    def add_lead(self, item: dict) -> dict | httpx.Response:
        stage = self.stage_of(item["status_id"]) if item.get("status_id") else self.amo["stages"][0]
        if stage is None:
            return _problem(400, "Bad Request", errors=[{"path": "status_id", "detail": "нет такого этапа, см. leads/pipelines"}])
        contacts = (item.get("_embedded") or {}).get("contacts") or []
        deal = {"id": self.next_id(), "name": str(item.get("name") or ""), "price": float(item.get("price") or 0),
                "contact_id": int(contacts[0]["id"]) if contacts and contacts[0].get("id") else None,
                "stage": stage, "responsible": self.user_name(item.get("responsible_user_id")) or self.amo["me"],
                "notes": []}
        self.amo["deals"].append(deal)
        return deal

    def patch_lead(self, deal_id: str, item: Any) -> httpx.Response:
        deal = next((d for d in self.amo["deals"] if str(d["id"]) == deal_id), None)
        if deal is None:
            return _problem(404, "Not Found")
        item = item or {}
        if "status_id" in item:
            stage = self.stage_of(item["status_id"])
            if stage is None:
                return _problem(400, "Bad Request", errors=[{"path": "status_id", "detail": "нет такого этапа, см. leads/pipelines"}])
            deal["stage"] = stage
        if "price" in item:
            deal["price"] = float(item["price"])
        if "name" in item:
            deal["name"] = str(item["name"])
        if "responsible_user_id" in item:
            deal["responsible"] = self.user_name(item["responsible_user_id"]) or deal["responsible"]
        return _response(200, {"id": deal["id"], "updated_at": 1762070400})
