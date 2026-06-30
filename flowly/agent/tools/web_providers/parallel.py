"""Parallel.ai search + content extraction provider.

Sync search (``Parallel``) + async extract (``AsyncParallel``); the SDK is
lazy-imported. Key comes from the connections card
(``tools.web.search.parallel.api_key``) or ``PARALLEL_API_KEY``. The search
mode is read from ``PARALLEL_SEARCH_MODE`` (default "agentic").
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from flowly.agent.tools.web_providers._config import provider_section
from flowly.agent.tools.web_providers.base import WebSearchProvider


def _parallel_key() -> str:
    section = provider_section("parallel")
    return ((getattr(section, "api_key", "") or "") or os.getenv("PARALLEL_API_KEY", "")).strip()


def _resolve_mode() -> str:
    mode = os.getenv("PARALLEL_SEARCH_MODE", "agentic").lower().strip()
    return mode if mode in {"fast", "one-shot", "agentic"} else "agentic"


def _get_sync_client() -> Any:
    key = _parallel_key()
    if not key:
        raise ValueError("PARALLEL_API_KEY is not set")
    from parallel import Parallel  # noqa: WPS433 — deliberately lazy

    return Parallel(api_key=key)


def _get_async_client() -> Any:
    key = _parallel_key()
    if not key:
        raise ValueError("PARALLEL_API_KEY is not set")
    from parallel import AsyncParallel  # noqa: WPS433 — deliberately lazy

    return AsyncParallel(api_key=key)


class ParallelWebSearchProvider(WebSearchProvider):
    """Parallel.ai search + async extract."""

    @property
    def name(self) -> str:
        return "parallel"

    @property
    def display_name(self) -> str:
        return "Parallel"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        section = provider_section("parallel")
        if not getattr(section, "enabled", False):
            return False
        return bool(_parallel_key())

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        try:
            response = _get_sync_client().beta.search(
                search_queries=[query],
                objective=query,
                mode=_resolve_mode(),
                max_results=min(int(limit), 20),
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except ImportError as exc:
            return {"success": False, "error": f"Parallel SDK not installed: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parallel search error: {}", exc)
            return {"success": False, "error": f"Parallel search failed: {exc}"}

        web = []
        for i, result in enumerate(response.results or []):
            excerpts = result.excerpts or []
            web.append({
                "url": result.url or "",
                "title": result.title or "",
                "description": " ".join(excerpts) if excerpts else "",
                "position": i + 1,
            })
        return {"success": True, "data": {"web": web}}

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        try:
            response = await _get_async_client().beta.extract(urls=urls, full_content=True)
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except ImportError as exc:
            return [{"url": u, "title": "", "content": "", "error": f"Parallel SDK not installed: {exc}"} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parallel extract error: {}", exc)
            return [{"url": u, "title": "", "content": "", "error": f"Parallel extract failed: {exc}"} for u in urls]

        results: list[dict[str, Any]] = []
        for result in response.results or []:
            content = result.full_content or ""
            if not content:
                content = "\n\n".join(result.excerpts or [])
            url = result.url or ""
            title = result.title or ""
            results.append({
                "url": url,
                "title": title,
                "content": content,
                "raw_content": content,
                "metadata": {"sourceURL": url, "title": title},
            })
        for error in getattr(response, "errors", None) or []:
            results.append({
                "url": getattr(error, "url", "") or "",
                "title": "",
                "content": "",
                "error": getattr(error, "content", None) or getattr(error, "error_type", None) or "extraction failed",
            })
        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Parallel",
            "badge": "paid · search + extract",
            "tag": "Objective-tuned search + parallel page extraction.",
            "env_vars": [
                {"key": "PARALLEL_API_KEY", "prompt": "Parallel API key", "url": "https://parallel.ai"},
            ],
        }
