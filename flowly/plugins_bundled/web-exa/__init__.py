"""web-exa plugin — registers Exa as a web_search/web_extract backend."""

from __future__ import annotations

from flowly.agent.tools.web_providers.exa import ExaWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(ExaWebSearchProvider())
