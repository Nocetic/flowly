"""SearXNG search provider.

Search-only — SearXNG aggregates upstream engines but does not fetch/extract
arbitrary URLs. Points at a user-hosted instance via the connections card
(``tools.web.search.searxng.url``) or the ``SEARXNG_URL`` env var.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.web_providers._config import provider_section
from flowly.agent.tools.web_providers.base import WebSearchProvider


def _searxng_url() -> str:
    """Resolve the SearXNG instance URL from config, then the env var."""
    section = provider_section("searxng")
    url = (getattr(section, "url", "") or "").strip()
    if not url:
        url = os.getenv("SEARXNG_URL", "").strip()
    return url


class SearXNGWebSearchProvider(WebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return "SearXNG"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def is_available(self) -> bool:
        section = provider_section("searxng")
        if not getattr(section, "enabled", False):
            return False
        return bool(_searxng_url())

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        base_url = _searxng_url().rstrip("/")
        if not base_url:
            return {"success": False, "error": "SearXNG URL is not set"}

        try:
            resp = httpx.get(
                f"{base_url}/search",
                params={"q": query, "format": "json", "pageno": 1},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error: {}", exc)
            return {"success": False, "error": f"SearXNG returned HTTP {exc.response.status_code}"}
        except httpx.RequestError as exc:
            logger.warning("SearXNG request error: {}", exc)
            return {"success": False, "error": f"Could not reach SearXNG at {base_url}: {exc}"}

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearXNG parse error: {}", exc)
            return {"success": False, "error": "Could not parse SearXNG response as JSON"}

        raw = data.get("results", []) or []
        ranked = sorted(raw, key=lambda r: float(r.get("score", 0) or 0), reverse=True)[:limit]
        web = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("content", "")),
                "position": i + 1,
            }
            for i, r in enumerate(ranked)
        ]
        logger.info("SearXNG search '{}': {} results", query, len(web))
        return {"success": True, "data": {"web": web}}

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "SearXNG",
            "badge": "free · self-hosted",
            "tag": "Privacy-respecting metasearch. Point at your instance URL.",
            "env_vars": [
                {"key": "SEARXNG_URL", "prompt": "SearXNG instance URL", "url": "https://searx.space/"},
            ],
        }
