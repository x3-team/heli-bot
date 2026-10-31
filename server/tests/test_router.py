import json
from decimal import Decimal

import pytest

from app.llm.client import Completion, HeliClient, HeliError
from app.llm.prices import cost_rub
from app.llm.router import Router, choose_alias


class _FakeCompletions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class Message:
            content = self.payload["text"]

        class Choice:
            message = Message()

        class Usage:
            prompt_tokens = self.payload["input_tokens"]
            completion_tokens = self.payload["output_tokens"]

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


class _FakeSDK:
    def __init__(self, payload: dict) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(payload)


def test_cost_uses_alias_prices() -> None:
    assert cost_rub("heli-fast", 1_000_000, 0) == Decimal("11.000000")
    assert cost_rub("heli-fast", 0, 1_000_000) == Decimal("22.000000")
    assert cost_rub("heli-embed", 1_000_000, 0) == Decimal("3.000000")


def test_router_rules() -> None:
    full = Decimal("1")
    low = Decimal("0.19")
    assert choose_alias(phase="execute", complexity=1, needs_vision=False, consecutive_errors=0, quota_remaining=full) == "heli-fast"
    assert choose_alias(phase="execute", complexity=1, needs_vision=True, consecutive_errors=0, quota_remaining=full) == "heli-vision"
    assert choose_alias(phase="plan", complexity=3, needs_vision=False, consecutive_errors=0, quota_remaining=full) == "heli-smart-alt"
    assert choose_alias(phase="execute", complexity=3, needs_vision=False, consecutive_errors=2, quota_remaining=full) == "heli-smart"
    assert choose_alias(phase="plan", complexity=3, needs_vision=False, consecutive_errors=2, quota_remaining=low) == "heli-fast"
    assert choose_alias(phase="execute", complexity=1, needs_vision=True, consecutive_errors=0, quota_remaining=low) == "heli-vision"


def test_completion_reports_alias_not_transport_model() -> None:
    sdk = _FakeSDK({"text": '{"task_type":"mail","complexity":2}', "input_tokens": 20, "output_tokens": 8})
    client = HeliClient(sdk)
    router = Router(client)
    task_type, complexity, completion = router.classify("отправь письмо")
    assert task_type == "mail"
    assert complexity == 2
    assert completion.alias == "heli-fast"
    sent = sdk.chat.completions.calls[0]["model"]
    assert sent not in repr(completion)
    assert "heli-fast" in repr(completion)
    assert completion.cost_rub == cost_rub("heli-fast", 20, 8)


def test_bad_json_defaults_to_complexity_two() -> None:
    sdk = _FakeSDK({"text": "не json", "input_tokens": 1, "output_tokens": 1})
    task_type, complexity, _ = Router(HeliClient(sdk)).classify("задача")
    assert task_type == "task"
    assert complexity == 2


def test_missing_key_has_fixed_error(monkeypatch) -> None:
    monkeypatch.delenv("HELI_API_KEY", raising=False)
    with pytest.raises(HeliError) as caught:
        HeliClient.from_env()
    assert caught.value.code == "missing_api_key"


def test_getheli_router_smoke() -> None:
    import os

    if not os.environ.get("HELI_API_KEY"):
        if os.environ.get("CI"):
            pytest.fail("HELI_API_KEY is not set")
        pytest.skip("HELI_API_KEY is not set")
    client = HeliClient.from_env()
    try:
        completion = client.complete(
            "heli-fast",
            [{"role": "user", "content": "Ответь одним словом: ок"}],
            max_tokens=16,
        )
    except HeliError as exc:
        pytest.fail(exc.code)
    assert completion.alias == "heli-fast"
    assert completion.input_tokens >= 0
    blob = json.dumps(
        {
            "alias": completion.alias,
            "text": completion.text,
            "cost": str(completion.cost_rub),
        },
        ensure_ascii=False,
    )
    secret = os.environ.get("HELI_API_KEY") or ""
    if secret and secret in blob:
        raise AssertionError("credential appeared in the model result")
    if secret and secret in repr(completion):
        raise AssertionError("credential appeared in the model result")
