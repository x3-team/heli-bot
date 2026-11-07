"""getheli chat client. The key is read from HELI_API_KEY and never logged."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
from openai import NOT_GIVEN, OpenAI

from app.llm.catalog import CatalogError, upstream
from app.llm.prices import cost_rub

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


class HeliError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        # Why the gateway refused (HTTP status, error type, short message). Never contains the key.
        self.detail = detail


def failure_detail(exc: BaseException) -> str:
    """«402 PermissionDeniedError: …» for logs and the CI smoke: the code alone hides the reason."""
    status = getattr(exc, "status_code", None)
    text = " ".join(str(exc).split())[:200]
    key = os.environ.get("HELI_API_KEY", "")
    if key and len(key) >= 8:
        text = text.replace(key, "<KEY>")
    cause = exc.__cause__ or exc.__context__
    # APITimeoutError alone does not say whether the connection or the answer was slow.
    why = f" ({type(cause).__name__})" if cause is not None and type(cause).__name__ != type(exc).__name__ else ""
    return " ".join(part for part in (str(status) if status else "", f"{type(exc).__name__}{why}: {text}") if part)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Completion:
    alias: str
    text: str
    input_tokens: int
    output_tokens: int
    cost_rub: Decimal
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Extra assistant fields some models need echoed back on the next request
    # (reasoning traces during tool use).
    extra: dict = field(default_factory=dict)

    def assistant_message(self) -> dict:
        message: dict = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in self.tool_calls
            ]
        message.update(self.extra)
        return message

    def __repr__(self) -> str:
        return (
            f"Completion(alias={self.alias!r}, input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens}, cost_rub={self.cost_rub})"
        )


# One model call waits at most this long (the SDK default is 10 minutes: a hung upstream held the run,
# its browser and the app's stream). HELI_LLM_TIMEOUT_S=0 keeps the SDK default.
LLM_TIMEOUT_S = 180.0
LLM_CONNECT_S = 15.0


def default_timeout() -> httpx.Timeout | None:
    raw = os.environ.get("HELI_LLM_TIMEOUT_S", "").strip()
    try:
        seconds = float(raw) if raw else LLM_TIMEOUT_S
    except ValueError:
        seconds = LLM_TIMEOUT_S
    if seconds <= 0:
        return None
    return httpx.Timeout(seconds, connect=min(LLM_CONNECT_S, seconds))


class HeliClient:
    def __init__(self, client: OpenAI) -> None:
        self._client = client

    @classmethod
    def from_env(cls, max_retries: int = 0, timeout: float | httpx.Timeout | None = None) -> HeliClient:
        key = os.environ.get("HELI_API_KEY", "")
        if not key:
            raise HeliError("missing_api_key")
        base_url = os.environ.get("HELI_BASE_URL", "https://getheli.ru/v1")
        if timeout is None:
            timeout = default_timeout()
        return cls(OpenAI(api_key=key, base_url=base_url, max_retries=max_retries,
                          timeout=NOT_GIVEN if timeout is None else timeout))

    def complete(
        self,
        alias: str,
        messages: list[dict],
        max_tokens: int = 256,
        tools: list[dict] | None = None,
    ) -> Completion:
        try:
            model = upstream(alias)
        except CatalogError as exc:
            raise HeliError(exc.code) from None
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0,
                **kwargs,
            )
        except Exception as exc:
            detail = failure_detail(exc)
            log.info("model request failed alias=%s %s", alias, detail)
            raise HeliError("request_failed", detail) from None
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        text = ""
        calls: list[ToolCall] = []
        extra: dict = {}
        if response.choices:
            message = response.choices[0].message
            text = message.content or ""
            for call in getattr(message, "tool_calls", None) or []:
                function = getattr(call, "function", None)
                if function is None:
                    continue
                calls.append(
                    ToolCall(
                        id=str(getattr(call, "id", "") or f"call_{len(calls)}"),
                        name=str(getattr(function, "name", "") or ""),
                        arguments=str(getattr(function, "arguments", "") or "{}"),
                    )
                )
            if calls:
                for key in ("reasoning_content", "reasoning", "reasoning_details"):
                    value = getattr(message, key, None)
                    if value is None:
                        value = (getattr(message, "model_extra", None) or {}).get(key)
                    if value:
                        extra[key] = value
        return Completion(
            alias=alias,
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_rub=cost_rub(alias, input_tokens, output_tokens),
            tool_calls=calls,
            extra=extra,
        )

    def embed(self, text: str) -> list[float]:
        try:
            model = upstream("heli-embed")
        except CatalogError as exc:
            raise HeliError(exc.code) from None
        try:
            response = self._client.embeddings.create(model=model, input=text)
        except Exception as exc:
            detail = failure_detail(exc)
            log.info("embedding request failed %s", detail)
            raise HeliError("request_failed", detail) from None
        data = getattr(response, "data", None) or []
        if not data:
            raise HeliError("request_failed")
        return [float(value) for value in data[0].embedding]
