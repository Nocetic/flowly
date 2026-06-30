"""web-tavily plugin — registers Tavily as a web_search/web_extract backend."""

from __future__ import annotations

from flowly.agent.tools.web_providers.tavily import TavilyWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(TavilyWebSearchProvider())
