"""Alias to upstream slug. Do not import this into API schemas or the client."""

from __future__ import annotations

import os

from app.aliases import PUBLIC_MODEL_ALIASES

# Slugs stay in this module. They are sent only on the server-to-gateway request.
_DEFAULTS = {
    "heli-fast": "deepseek/deepseek-v4-flash",
    "heli-fast-alt": "openai/gpt-5.4-mini",
    "heli-vision": "google/gemini-3.7-flash",
    "heli-smart": "anthropic/claude-sonnet-5",
    "heli-smart-alt": "deepseek/deepseek-v4-pro",
    "heli-embed": "openai/text-embedding-3-small",
}

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


def upstream(alias: str) -> str:
    if alias not in PUBLIC_MODEL_ALIASES and alias != "heli-embed":
        raise CatalogError("unknown_alias")
    override = os.environ.get(_ENV.get(alias, ""), "")
    if override:
        return override
    slug = _DEFAULTS.get(alias)
    if not slug:
        raise CatalogError("alias_not_configured")
    return slug
