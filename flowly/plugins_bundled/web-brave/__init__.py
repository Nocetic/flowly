"""web-brave plugin — registers Brave Search as a ``web_search`` backend.

Brave is Flowly's default backend, so its provider class lives in core
(:mod:`flowly.agent.tools.web_providers.brave`) and the ``web_search`` tool
can fall back to it directly when plugin discovery hasn't run. This plugin
makes it appear in ``/plugins`` and participate in backend resolution like
every other provider.
"""

from __future__ import annotations

from flowly.agent.tools.web_providers.brave import BraveWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(BraveWebSearchProvider())
