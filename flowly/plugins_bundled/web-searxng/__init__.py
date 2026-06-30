"""web-searxng plugin — registers SearXNG as a web_search backend."""

from __future__ import annotations

from flowly.agent.tools.web_providers.searxng import SearXNGWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(SearXNGWebSearchProvider())
