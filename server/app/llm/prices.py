"""Rubles per 1M tokens, keyed only by public alias. Updated by hand."""

from decimal import Decimal

from app.llm.catalog import preset

# (input, output). Embeddings have no output price.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "heli-fast": (Decimal("11"), Decimal("22")),
    "heli-fast-alt": (Decimal("101"), Decimal("603.5")),
    "heli-vision": (Decimal("50.5"), Decimal("251.5")),
    "heli-vision-lite": (Decimal("29.5"), Decimal("88.5")),
    "heli-smart": (Decimal("275.5"), Decimal("1377")),
    "heli-smart-alt": (Decimal("117"), Decimal("233.5")),
    "heli-embed": (Decimal("3"), Decimal("0")),
}

# HELI_MODELS=ru: Cloud.ru Evolution Foundation Models price list v260915, VAT 22% included.
RU_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "heli-fast": (Decimal("15.86"), Decimal("61")),  # gpt-oss-120b
    "heli-fast-alt": (Decimal("219.6"), Decimal("329.4")),  # Qwen3.6-35B-A3B
    "heli-vision": (Decimal("219.6"), Decimal("329.4")),
    "heli-vision-lite": (Decimal("219.6"), Decimal("329.4")),
    "heli-smart": (Decimal("198.86"), Decimal("829.6")),  # GLM-5.1
    "heli-smart-alt": (Decimal("183"), Decimal("732")),  # DeepSeek-V4-Pro
    "heli-embed": (Decimal("0.61"), Decimal("0")),  # bge-m3
}


def price_table() -> dict[str, tuple[Decimal, Decimal]]:
    return RU_PRICES if preset() == "ru" else PRICES


def cost_rub(alias: str, input_tokens: int, output_tokens: int) -> Decimal:
    table = price_table()
    if alias not in table:
        raise KeyError(alias)
    input_price, output_price = table[alias]
    total = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal(1_000_000)
    return total.quantize(Decimal("0.000001"))
