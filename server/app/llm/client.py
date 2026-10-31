"""getheli chat client. The key is read from HELI_API_KEY and never logged."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

from openai import OpenAI

from app.llm.catalog import CatalogError, upstream
from app.llm.prices import cost_rub

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


class HeliError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Completion:
    alias: str
    text: str
    input_tokens: int
    output_tokens: int
    cost_rub: Decimal

    def __repr__(self) -> str:
        return (
            f"Completion(alias={self.alias!r}, input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens}, cost_rub={self.cost_rub})"
        )


class HeliClient:
    def __init__(self, client: OpenAI) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> HeliClient:
        key = os.environ.get("HELI_API_KEY", "")
        if not key:
            raise HeliError("missing_api_key")
        base_url = os.environ.get("HELI_BASE_URL", "https://getheli.ru/v1")
        return cls(OpenAI(api_key=key, base_url=base_url, max_retries=0))

    def complete(self, alias: str, messages: list[dict], max_tokens: int = 256) -> Completion:
        try:
            model = upstream(alias)
        except CatalogError as exc:
            raise HeliError(exc.code) from None
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0,
            )
        except Exception:
            log.info("model request failed alias=%s", alias)
            raise HeliError("request_failed") from None
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        text = ""
        if response.choices:
            text = response.choices[0].message.content or ""
        return Completion(
            alias=alias,
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_rub=cost_rub(alias, input_tokens, output_tokens),
        )

    def embed(self, text: str) -> list[float]:
        try:
            model = upstream("heli-embed")
        except CatalogError as exc:
            raise HeliError(exc.code) from None
        try:
            response = self._client.embeddings.create(model=model, input=text)
        except Exception:
            log.info("embedding request failed")
            raise HeliError("request_failed") from None
        data = getattr(response, "data", None) or []
        if not data:
            raise HeliError("request_failed")
        return [float(value) for value in data[0].embedding]
