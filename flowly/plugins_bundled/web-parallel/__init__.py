"""web-parallel plugin — registers Parallel as a web_search/web_extract backend."""

from __future__ import annotations

from flowly.agent.tools.web_providers.parallel import ParallelWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(ParallelWebSearchProvider())
