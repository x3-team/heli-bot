"""Разбор адресов без сети и DNS."""

from __future__ import annotations

from urllib.parse import urlsplit


def host_of(url: str | None) -> str:
    if not url:
        return ""
    raw = url if "://" in url else f"https://{url}"
    try:
        return (urlsplit(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def host_matches(host: str, suffix: str) -> bool:
    suffix = suffix.lower().lstrip(".")
    return bool(host) and (host == suffix or host.endswith(f".{suffix}"))
