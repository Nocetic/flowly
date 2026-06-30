"""web-local plugin — registers the local readability extract backend.

Extract-only, keyless, always available — the ``web_extract`` fallback when
no paid extractor is configured. Its provider class lives in core
(:mod:`flowly.agent.tools.web_providers.local`) since the ``web_extract`` tool
falls back to it directly.
"""

from __future__ import annotations

from flowly.agent.tools.web_providers.local import LocalExtractProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(LocalExtractProvider())
