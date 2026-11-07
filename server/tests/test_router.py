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
    import httpx

    # A live call from a GitHub runner to getheli.ru: the network on the way, not only the gateway.
    # Two retries and a longer connect, so one slow connection is not a red main; a real refusal still fails.
    client = HeliClient.from_env(max_retries=2, timeout=httpx.Timeout(60.0, connect=20.0))
    try:
        completion = client.complete(
            "heli-fast",
            [{"role": "user", "content": "Ответь одним словом: ок"}],
            max_tokens=16,
        )
    except HeliError as exc:
        pytest.fail(f"{exc.code}: {exc.detail}")
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


def test_failure_detail_names_the_status_and_hides_the_key(monkeypatch) -> None:
    from app.llm.client import failure_detail

    monkeypatch.setenv("HELI_API_KEY", "sk-heli-secret-123456")

    class PaymentRequired(Exception):
        status_code = 402

    detail = failure_detail(PaymentRequired("Недостаточно средств для ключа sk-heli-secret-123456"))
    assert detail.startswith("402 PaymentRequired:") and "sk-heli-secret" not in detail and "<KEY>" in detail


class _FlakyCompletions(_FakeCompletions):
    """Fails with the given exceptions first, then answers."""

    def __init__(self, payload: dict, failures: list) -> None:
        super().__init__(payload)
        self.failures = list(failures)
        self.attempts = 0

    def create(self, **kwargs):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return super().create(**kwargs)


def _flaky(failures: list, retries: int) -> tuple[HeliClient, _FlakyCompletions, list]:
    sdk = _FakeSDK({"text": "ок", "input_tokens": 1, "output_tokens": 1})
    sdk.chat.completions = _FlakyCompletions({"text": "ок", "input_tokens": 1, "output_tokens": 1}, failures)
    pauses: list = []
    return HeliClient(sdk, retries=retries, sleep=pauses.append), sdk.chat.completions, pauses


def _status_error(status: int):
    import httpx
    import openai

    request = httpx.Request("POST", "https://getheli.ru/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": "x"}})
    cls = {402: openai.PermissionDeniedError, 401: openai.AuthenticationError}.get(status, openai.InternalServerError)
    if status == 429:
        cls = openai.RateLimitError
    return cls("gateway said no", response=response, body=None)


def _connection_error():
    import httpx
    import openai

    return openai.APIConnectionError(request=httpx.Request("POST", "https://getheli.ru/v1/chat/completions"))


def test_reset_connection_is_retried_and_the_run_goes_on() -> None:
    client, completions, pauses = _flaky([_connection_error(), _status_error(502)], retries=2)
    completion = client.complete("heli-fast", [{"role": "user", "content": "привет"}])
    assert completion.text == "ок"
    assert completions.attempts == 3
    assert pauses == [1.0, 3.0]


def test_rate_limit_is_retried() -> None:
    client, completions, _ = _flaky([_status_error(429)], retries=2)
    assert client.complete("heli-fast", [{"role": "user", "content": "привет"}]).text == "ок"
    assert completions.attempts == 2


def test_refused_key_or_balance_is_not_retried() -> None:
    for status in (401, 402):
        client, completions, pauses = _flaky([_status_error(status)], retries=2)
        with pytest.raises(HeliError) as caught:
            client.complete("heli-fast", [{"role": "user", "content": "привет"}])
        assert caught.value.code == "request_failed"
        assert str(status) in caught.value.detail
        assert completions.attempts == 1
        assert pauses == []


def test_retries_end_with_request_failed_and_the_reason() -> None:
    client, completions, _ = _flaky([_connection_error()] * 5, retries=2)
    with pytest.raises(HeliError) as caught:
        client.complete("heli-fast", [{"role": "user", "content": "привет"}])
    assert completions.attempts == 3
    assert "APIConnectionError" in caught.value.detail


def test_from_env_retries_twice_by_default(monkeypatch) -> None:
    monkeypatch.setenv("HELI_API_KEY", "test-key-123456")
    monkeypatch.delenv("HELI_LLM_RETRIES", raising=False)
    client = HeliClient.from_env()
    assert client._retries == 2
    assert client._client.max_retries == 0
    monkeypatch.setenv("HELI_LLM_RETRIES", "0")
    assert HeliClient.from_env()._retries == 0
