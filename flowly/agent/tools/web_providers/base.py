"""Web search/extract provider interface.

Flowly's web tools (``web_search`` / ``web_extract``) dispatch every call to
a pluggable backend that implements :class:`WebSearchProvider`. Backends
register themselves through the plugin system — a bundled or user plugin
declares ``kind: backend`` and calls
``ctx.register_web_search_provider(...)`` from its ``register()`` entry
point (see :mod:`flowly.plugins.context`). The active provider for each
capability is selected from config (see
:mod:`flowly.agent.tools.web_providers.registry`).

This ABC is the single surface every provider implements (brave, ddgs,
searxng, exa, tavily, firecrawl, parallel, xai). Multi-capability
providers (Firecrawl, Tavily, Exa, …) advertise both ``supports_search``
and ``supports_extract`` from one class.

Response shapes
---------------

Search results::

    {
        "success": True,
        "data": {
            "web": [
                {"title": str, "url": str, "description": str, "position": int},
                ...
            ]
        }
    }

Extract results::

    [
        {"url": str, "title": str, "content": str,
         "raw_content": str, "metadata": dict},
        ...
    ]

On failure::

    search  -> {"success": False, "error": str}
    extract -> [{"url": str, "title": "", "content": "", "error": str}, ...]
"""

from __future__ import annotations

import abc
from typing import Any


class WebSearchProvider(abc.ABC):
    """Abstract base class for a web search/extract backend.

    Subclasses must implement :meth:`is_available` and at least one of
    :meth:`search` / :meth:`extract`. The :meth:`supports_search` /
    :meth:`supports_extract` flags let the registry route each tool call
    to a backend that can service it.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in the ``backend`` /
        ``searchBackend`` / ``extractBackend`` config keys.

        Lowercase, no spaces; hyphens permitted. Examples: ``brave``,
        ``ddgs``, ``searxng``, ``firecrawl``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label. Defaults to :attr:`name`."""
        return self.name

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Must be a cheap check (env var present, optional dependency
        importable, instance URL set) and MUST NOT make network calls —
        it runs at resolution time on every web tool call.
        """

    def supports_search(self) -> bool:
        """Return True if this provider implements :meth:`search`."""
        return True

    def supports_extract(self) -> bool:
        """Return True if this provider implements :meth:`extract`.

        Both sync and async :meth:`extract` implementations are valid —
        the dispatcher detects coroutine functions via
        :func:`inspect.iscoroutinefunction` and awaits as needed.
        """
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a web search.

        Override when :meth:`supports_search` returns True. Callers gate
        on :meth:`supports_search` before calling.
        """
        raise NotImplementedError(
            f"{self.name} does not support search (override supports_search)"
        )

    def extract(self, urls: list[str], **kwargs: Any) -> Any:
        """Extract content from one or more URLs.

        Override when :meth:`supports_extract` returns True. Returns a
        list of result dicts (see the module docstring for the shape).
        Implementations MAY be ``async def``. ``kwargs`` may carry
        forward-compat fields (``format``, ``query``, ``max_chars``) —
        implementations should ignore unknown keys.
        """
        raise NotImplementedError(
            f"{self.name} does not support extract (override supports_extract)"
        )

    def get_setup_schema(self) -> dict[str, Any]:
        """Return provider metadata for setup/listing UIs.

        Shape::

            {
                "name": "Brave Search",
                "badge": "free",
                "tag": "Short description.",
                "env_vars": [
                    {"key": "BRAVE_API_KEY",
                     "prompt": "Brave Search API key",
                     "url": "https://brave.com/search/api/"},
                ],
            }
        """
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }
