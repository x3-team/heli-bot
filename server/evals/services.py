"""Вымышленные внешние сервисы для цикла «освой инструмент».

``http_request`` в мире — прод-инструмент без сети: адрес, политика «бой только чтение, запись только
в песочнице», подстановка ``{{secret:ИМЯ}}`` и маскирование ключей идут через ``app.agent.http_tool``
(``prepare`` / ``format_response``), а вместо отправки запрос отвечает мок по ``state["services"]``.
DNS тоже мок: адрес из ``services.dns`` или публичный, так что запрет частных адресов проверяется по-настоящему.

``knowledge_search`` / ``knowledge_save`` — те же схемы, что в проде, поверх ``state["knowledge"]``
(запись: ``topic``, ``fact``, ``source_url``, ``checked`` — дата мира).

``state["services"]``:

* ``sandbox_hosts`` — песочницы стенда, как ``HELI_HTTP_SANDBOX_HOSTS`` на сервере;
* ``vault`` — «Доступы»: ``[{"site", "secret"}]``;
* ``dns`` — ``{host: ip}``;
* ``endpoints`` — ``[{"method", "host", "path", "auth"?, "status"?, "body", "append"?, "echo_auth"?}]``.
  ``auth`` — секрет, который ждёт сервис (иначе 401), ``append`` — путь к списку в state, куда POST
  кладёт тело запроса (так видно, ушла ли запись), ``echo_auth`` — небрежный API, который возвращает ключ.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from app import knowledge as _kn
from app.agent import http_tool as _http

TOOLS: list[dict] = [copy.deepcopy(_http.TOOL), copy.deepcopy(_kn.SEARCH_TOOL), copy.deepcopy(_kn.SAVE_TOOL)]
PUBLIC_IP = "93.184.216.34"


@contextmanager
def _sandbox_env(hosts: list[str]):
    """Песочницы стенда поверх HELI_HTTP_SANDBOX_HOSTS — на время одного вызова."""
    old = os.environ.get("HELI_HTTP_SANDBOX_HOSTS")
    merged = ",".join(h for h in [old or "", *hosts] if h)
    os.environ["HELI_HTTP_SANDBOX_HOSTS"] = merged
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HELI_HTTP_SANDBOX_HOSTS", None)
        else:
            os.environ["HELI_HTTP_SANDBOX_HOSTS"] = old


def _list_at(state: dict, path: str) -> list:
    node: Any = state
    for part in path.split("."):
        node = node[part]
    return node


class ServiceTools:
    """Примесь к evals.world.World: инструменты незнакомых сервисов."""

    state: dict

    def _services(self) -> dict:
        return self.state.setdefault("services", {})

    def _dns(self, host: str, port: int) -> list:
        try:
            ip = str(ipaddress.ip_address(host.strip("[]")))  # an address literal resolves to itself
        except ValueError:
            ip = self._services().get("dns", {}).get(host.lower(), PUBLIC_IP)
        return [(0, 0, 0, "", (ip, port))]

    def _lookup(self, name: str):
        rows = [{"site": v["site"], "username": v.get("login", ""), "secret": v["secret"]}
                for v in self._services().get("vault", [])]
        row = _http._find_secret(rows, name)
        return (row["site"], row["secret"]) if row else None

    def _t_http_request(self, method: str, url: str, query: dict | None = None, headers: dict | None = None,
                        **rest: Any) -> dict:
        from evals.world import ToolError

        unknown = set(rest) - {"json"}
        if unknown:
            raise TypeError(f"unexpected arguments: {', '.join(sorted(unknown))}")
        args = {"method": method, "url": url, "query": query or {}, "headers": headers or {}, "json": rest.get("json")}
        services = self._services()
        secrets: list[str] = []
        try:
            with _sandbox_env(services.get("sandbox_hosts", [])):
                req, secrets = _http.prepare(args, self._lookup, resolver=self._dns)
        except _http.HttpToolError as exc:
            raise ToolError(f"{exc.code}: {_http.redact(exc.text, secrets)}") from None
        status, body = self._answer(req, secrets)
        request = httpx.Request(req.method, req.url, params=req.query or None)
        response = httpx.Response(status, json=body, request=request)
        text = _http.format_response(response, req.mode, secrets)
        if status >= 400:
            raise ToolError(f"http_{status}: {text}")
        return {"content": text}

    def _answer(self, req: _http.Prepared, secrets: list[str]) -> tuple[int, Any]:
        path = urlsplit(req.url).path or "/"
        for ep in self._services().get("endpoints", []):
            if ep["method"] != req.method or ep["host"] != req.host or ep["path"].rstrip("/") != path.rstrip("/"):
                continue
            sent = json.dumps([req.headers, req.query], ensure_ascii=False)
            if ep.get("auth") and ep["auth"] not in sent:
                return 401, {"error": "unauthorized", "message": "Неверный или просроченный токен"}
            body = copy.deepcopy(ep.get("body", {}))
            if ep.get("append"):
                target = _list_at(self.state, ep["append"])
                item = {"id": f"{ep.get('id_prefix', 'ID-')}{len(target) + 501}", **(req.body or {})}
                target.append(item)
                body = {**body, **item}
            if ep.get("echo_auth"):
                body["echo"] = req.headers
            return int(ep.get("status", 200)), body
        return 404, {"error": "not_found"}

    def _t_knowledge_search(self, query: str) -> dict:
        from evals.world import _ranked

        notes = _ranked(query, self.state.get("knowledge", []), lambda n: (n["topic"], n["fact"]))
        if not notes:
            return {"notes": [], "hint": "В базе знаний ничего нет по этой теме. Найди официальную справку "
                                         "или документацию (web_search, open_url)."}
        return {"notes": notes[:5]}

    def _t_knowledge_save(self, topic: str, fact: str, source_url: str) -> dict:
        from evals.world import ToolError

        if not str(source_url).strip().lower().startswith(("https://", "http://")):
            raise ToolError("no_source: нужен адрес источника https://…")
        if not str(topic).strip() or not str(fact).strip():
            raise ToolError("bad_args: укажи тему и вывод")
        note = {"topic": topic, "fact": fact, "source_url": source_url, "checked": self.state["today"]}
        self.state.setdefault("knowledge", []).append(note)
        return {"saved": True, **note}
