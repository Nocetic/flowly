"""Pluggable web search/extract providers.

The :class:`WebSearchProvider` ABC is the surface every backend implements;
:mod:`flowly.agent.tools.web_providers.registry` tracks registered providers
and resolves the active one per capability from config. Providers register
themselves from ``kind: backend`` plugins via
``ctx.register_web_search_provider(...)``.
"""

from __future__ import annotations

from flowly.agent.tools.web_providers.base import WebSearchProvider
from flowly.agent.tools.web_providers.registry import (
    get_active_extract_provider,
    get_active_search_provider,
    get_provider,
    list_providers,
    register_provider,
)

__all__ = [
    "WebSearchProvider",
    "register_provider",
    "get_provider",
    "list_providers",
    "get_active_search_provider",
    "get_active_extract_provider",
]
