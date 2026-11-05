from decimal import Decimal

import pytest

from app.llm import catalog
from app.llm.client import HeliClient, HeliError, sdk_for
from app.llm.prices import cost_rub
from app.masking import mask_text, redact_secrets_text
from app.memory import _rank


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("HELI_MODELS", "HELI_UPSTREAM_FAST", "CLOUDRU_API_KEY", "YANDEX_API_KEY", "YANDEX_FOLDER_ID"):
        monkeypatch.delenv(name, raising=False)


def test_default_preset_is_gateway() -> None:
    provider, model = catalog.resolve("heli-fast")
    assert provider.name == "gateway"
    assert model == "deepseek/deepseek-v4-flash"
    assert cost_rub("heli-fast", 1_000_000, 0) == Decimal("11.000000")


def test_ru_preset_uses_cloudru_and_ru_prices(monkeypatch) -> None:
    monkeypatch.setenv("HELI_MODELS", "ru")
    for alias in sorted(catalog.PUBLIC_MODEL_ALIASES | {"heli-embed"}):
        provider, model = catalog.resolve(alias)
        assert provider.name == "cloudru", alias
        assert ":" not in model
        assert cost_rub(alias, 1000, 1000) > 0
    assert cost_rub("heli-fast", 1_000_000, 0) == Decimal("15.860000")


def test_unknown_preset_fails(monkeypatch) -> None:
    monkeypatch.setenv("HELI_MODELS", "mars")
    with pytest.raises(catalog.CatalogError):
        catalog.upstream("heli-fast")


def test_override_with_provider_prefix(monkeypatch) -> None:
    monkeypatch.setenv("HELI_UPSTREAM_FAST", "yandex:gpt://{folder}/yandexgpt/latest")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gtest")
    provider, model = catalog.resolve("heli-fast")
    assert provider.name == "yandex"
    assert model == "gpt://b1gtest/yandexgpt/latest"


def test_missing_provider_key_is_fixed_error(monkeypatch) -> None:
    monkeypatch.setenv("HELI_MODELS", "ru")
    assert not catalog.configured("heli-embed")
    with pytest.raises(HeliError) as exc:
        HeliClient.from_env()
    assert exc.value.code == "missing_api_key"
    monkeypatch.setenv("CLOUDRU_API_KEY", "cloudru-secret-value")
    assert catalog.configured("heli-embed")
    sdk = sdk_for(catalog.PROVIDERS["cloudru"])
    assert str(sdk.base_url).startswith("https://foundation-models.api.cloud.ru/v1")


def test_all_provider_keys_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("CLOUDRU_API_KEY", "cloudru-secret-value")
    monkeypatch.setenv("YANDEX_API_KEY", "yandex-secret-value")
    text = "key cloudru-secret-value and yandex-secret-value"
    assert "secret-value" not in mask_text(text)
    assert "secret-value" not in redact_secrets_text(text)


def test_vectors_of_other_size_fall_back_to_words() -> None:
    class Row:
        text = "склад на Ленина"
        embedding = [0.1] * 1536

    assert _rank("склад Ленина", [0.2] * 1024, Row()) > 0
