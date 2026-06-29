"""Shared config access for web search providers.

Providers read their credentials/toggles from ``tools.web.search`` (the same
block the connections cards write to) with an env-var fallback. These helpers
keep that access defensive — a missing/unreadable config never raises, it
just resolves to "not configured".
"""

from __future__ import annotations

from typing import Any


def web_search_config() -> Any | None:
    """Return the ``tools.web.search`` config block, or None if unreadable."""
    try:
        from flowly.config.loader import load_config

        return load_config().tools.web.search
    except Exception:
        return None


def provider_section(name: str) -> Any | None:
    """Return the per-provider sub-block (``tools.web.search.<name>``) or None."""
    search = web_search_config()
    return getattr(search, name, None) if search is not None else None
