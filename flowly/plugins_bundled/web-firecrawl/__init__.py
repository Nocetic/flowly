"""web-firecrawl plugin — registers Firecrawl as a web_search/web_extract backend."""

from __future__ import annotations

from flowly.agent.tools.web_providers.firecrawl import FirecrawlWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(FirecrawlWebSearchProvider())
