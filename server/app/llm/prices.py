"""Rubles per 1M tokens, keyed only by public alias. Updated by hand."""

from decimal import Decimal

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


def cost_rub(alias: str, input_tokens: int, output_tokens: int) -> Decimal:
    if alias not in PRICES:
        raise KeyError(alias)
    input_price, output_price = PRICES[alias]
    total = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal(1_000_000)
    return total.quantize(Decimal("0.000001"))
