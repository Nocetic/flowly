"""web-ddgs plugin — registers DuckDuckGo (ddgs) as a web_search backend."""

from __future__ import annotations

from flowly.agent.tools.web_providers.ddgs import DDGSWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(DDGSWebSearchProvider())
