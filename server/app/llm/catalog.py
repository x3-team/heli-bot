"""Alias to upstream slug. Do not import this into API schemas or the client.

A slug is ``provider:model`` or a bare ``model`` (the getheli gateway). ``HELI_MODELS`` picks the
preset: ``gateway`` (default, foreign models via getheli) or ``ru`` (models hosted in Russia, no VPN,
data stays in the RF). ``HELI_UPSTREAM_*`` overrides one alias in either preset.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.aliases import PUBLIC_MODEL_ALIASES


@dataclass(frozen=True)
class Provider:
    name: str
    key_env: str
    url_env: str
    default_url: str


PROVIDERS = {
    "gateway": Provider("gateway", "HELI_API_KEY", "HELI_BASE_URL", "https://getheli.ru/v1"),
    # Cloud.ru Evolution Foundation Models, OpenAI-compatible, hosted in Moscow.
    "cloudru": Provider("cloudru", "CLOUDRU_API_KEY", "CLOUDRU_BASE_URL", "https://foundation-models.api.cloud.ru/v1"),
    # Yandex AI Studio, OpenAI-compatible. Model ids use the folder: gpt://{folder}/yandexgpt/latest.
    "yandex": Provider("yandex", "YANDEX_API_KEY", "YANDEX_BASE_URL", "https://llm.api.cloud.yandex.net/v1"),
}

# Slugs stay in this module. They are sent only on the server-to-provider request.
_GATEWAY = {
    "heli-fast": "deepseek/deepseek-v4-flash",
    "heli-fast-alt": "openai/gpt-5.4-mini",
    "heli-vision": "google/gemini-3.7-flash",
    "heli-smart": "anthropic/claude-sonnet-5",
    "heli-smart-alt": "deepseek/deepseek-v4-pro",
    "heli-embed": "openai/text-embedding-3-small",
}

# Open models that Cloud.ru runs itself (not its "partner" models, which are proxied abroad).
_RU = {
    "heli-fast": "cloudru:openai/gpt-oss-120b",
    "heli-fast-alt": "cloudru:Qwen/Qwen3.6-35B-A3B",
    "heli-vision": "cloudru:Qwen/Qwen3.6-35B-A3B",
    "heli-vision-lite": "cloudru:Qwen/Qwen3.6-35B-A3B",
    "heli-smart": "cloudru:zai-org/GLM-5.1",
    "heli-smart-alt": "cloudru:deepseek-ai/DeepSeek-V4-Pro",
    "heli-embed": "cloudru:BAAI/bge-m3",
}

PRESETS = {"gateway": _GATEWAY, "ru": _RU}

_ENV = {
    "heli-fast": "HELI_UPSTREAM_FAST",
    "heli-fast-alt": "HELI_UPSTREAM_FAST_ALT",
    "heli-vision": "HELI_UPSTREAM_VISION",
    "heli-vision-lite": "HELI_UPSTREAM_VISION_LITE",
    "heli-smart": "HELI_UPSTREAM_SMART",
    "heli-smart-alt": "HELI_UPSTREAM_SMART_ALT",
    "heli-embed": "HELI_UPSTREAM_EMBED",
}


class CatalogError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def preset() -> str:
    name = os.environ.get("HELI_MODELS", "").strip().lower() or "gateway"
    if name not in PRESETS:
        raise CatalogError("unknown_preset")
    return name


def upstream(alias: str) -> str:
    """Full slug for an alias, with the provider prefix if there is one."""
    if alias not in PUBLIC_MODEL_ALIASES and alias != "heli-embed":
        raise CatalogError("unknown_alias")
    override = os.environ.get(_ENV.get(alias, ""), "")
    if override:
        return override
    slug = PRESETS[preset()].get(alias)
    if not slug:
        raise CatalogError("alias_not_configured")
    return slug


def split(slug: str) -> tuple[Provider, str]:
    """``cloudru:openai/gpt-oss-120b`` -> (cloudru, ``openai/gpt-oss-120b``). No prefix means the gateway."""
    name, sep, model = slug.partition(":")
    if not sep or name not in PROVIDERS:
        return PROVIDERS["gateway"], slug
    if name == "yandex":
        model = model.replace("{folder}", os.environ.get("YANDEX_FOLDER_ID", ""))
    return PROVIDERS[name], model


def resolve(alias: str) -> tuple[Provider, str]:
    return split(upstream(alias))


def provider_key(provider: Provider) -> str:
    return os.environ.get(provider.key_env, "").strip()


def configured(alias: str) -> bool:
    """True when the provider behind this alias has a key."""
    try:
        provider, _ = resolve(alias)
    except CatalogError:
        return False
    return bool(provider_key(provider))


def secret_values() -> list[str]:
    """Every provider key that is set, for log and trajectory redaction."""
    return [value for value in (provider_key(p) for p in PROVIDERS.values()) if value]
