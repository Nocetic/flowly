"""DuckDuckGo search provider (via the ``ddgs`` package).

Search-only, no API key. The ``ddgs`` package is an optional dependency
(``pip install ddgs`` or ``flowly[search]``); ``is_available`` reflects both
the connections-card toggle and whether the package is importable.
"""

from __future__ import annotations

import concurrent.futures as _cf
from typing import Any

from loguru import logger

from flowly.agent.tools.web_providers._config import provider_section
from flowly.agent.tools.web_providers.base import WebSearchProvider

# Overall wall-clock cap for one search. DDGS's per-request timeout doesn't
# bound its multi-engine retry loop, so a rate-limited/slow response could
# otherwise hang the shared agent loop. Enforce a hard cap via a worker thread.
_SEARCH_TIMEOUT_SECS = 30


def _ddgs_importable() -> bool:
    try:
        import ddgs  # noqa: F401

        return True
    except ImportError:
        return False


def _run_ddgs_search(query: str, safe_limit: int) -> list[dict[str, Any]]:
    """Run the blocking ddgs query and return normalized hits.

    Module-level (not a closure) so tests can patch it directly without
    spawning a real multi-second worker thread.
    """
    from ddgs import DDGS  # type: ignore

    results: list[dict[str, Any]] = []
    with DDGS(timeout=10) as client:
        for i, hit in enumerate(client.text(query, max_results=safe_limit)):
            if i >= safe_limit:
                break
            url = str(hit.get("href") or hit.get("url") or "")
            results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                }
            )
    return results


class DDGSWebSearchProvider(WebSearchProvider):
    """DuckDuckGo HTML-scrape search. No API key; rate limits are server-side."""

    @property
    def name(self) -> str:
        return "ddgs"

    @property
    def display_name(self) -> str:
        return "DuckDuckGo (ddgs)"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def is_available(self) -> bool:
        section = provider_section("ddgs")
        if not getattr(section, "enabled", False):
            return False
        return _ddgs_importable()

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not _ddgs_importable():
            return {
                "success": False,
                "error": "ddgs package is not installed — run `pip install ddgs`",
            }

        safe_limit = max(1, int(limit))

        # A fresh single-worker pool per call: on timeout the blocking ddgs
        # call can't be cancelled and keeps running, so a shared pool would
        # serialise later searches behind the hung worker.
        pool = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_run_ddgs_search, query, safe_limit)
            try:
                web = future.result(timeout=_SEARCH_TIMEOUT_SECS)
            except _cf.TimeoutError:
                logger.warning("ddgs search timed out after {}s: {!r}", _SEARCH_TIMEOUT_SECS, query)
                return {
                    "success": False,
                    "error": (
                        f"DuckDuckGo search timed out after {_SEARCH_TIMEOUT_SECS}s — "
                        "it may be rate-limiting. Try again or switch provider."
                    ),
                }
        except Exception as exc:  # noqa: BLE001 — ddgs raises its own exceptions
            logger.warning("ddgs search error: {}", exc)
            return {"success": False, "error": f"DuckDuckGo search failed: {exc}"}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        logger.info("ddgs search '{}': {} results", query, len(web))
        return {"success": True, "data": {"web": web}}

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "DuckDuckGo (ddgs)",
            "badge": "free · no key",
            "tag": "Search via the ddgs package — no API key (pair with any extract provider).",
            "env_vars": [],
        }
