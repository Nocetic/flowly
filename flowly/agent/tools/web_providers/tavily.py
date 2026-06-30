"""Tavily search + content extraction provider.

Search + extract via the Tavily REST API (no SDK — plain httpx). Key comes
from the connections card (``tools.web.search.tavily.api_key``) or the
``TAVILY_API_KEY`` env var.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.web_providers._config import provider_section
from flowly.agent.tools.web_providers.base import WebSearchProvider


def _tavily_key() -> str:
    section = provider_section("tavily")
    return ((getattr(section, "api_key", "") or "") or os.getenv("TAVILY_API_KEY", "")).strip()


def _tavily_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = _tavily_key()
    if not key:
        raise ValueError("TAVILY_API_KEY is not set")
    base_url = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com")
    body = {**payload, "api_key": key}
    resp = httpx.post(f"{base_url}/{endpoint.lstrip('/')}", json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()


class TavilyWebSearchProvider(WebSearchProvider):
    """Tavily search + extract."""

    @property
    def name(self) -> str:
        return "tavily"

    @property
    def display_name(self) -> str:
        return "Tavily"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        section = provider_section("tavily")
        if not getattr(section, "enabled", False):
            return False
        return bool(_tavily_key())

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        try:
            raw = _tavily_request("search", {
                "query": query,
                "max_results": min(int(limit), 20),
                "include_raw_content": False,
                "include_images": False,
            })
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily search error: {}", exc)
            return {"success": False, "error": f"Tavily search failed: {exc}"}

        web = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("content", ""),
                "position": i + 1,
            }
            for i, r in enumerate(raw.get("results", []) or [])
        ]
        return {"success": True, "data": {"web": web}}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        try:
            raw = _tavily_request("extract", {"urls": urls, "include_images": False})
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily extract error: {}", exc)
            return [{"url": u, "title": "", "content": "", "error": f"Tavily extract failed: {exc}"} for u in urls]

        documents: list[dict[str, Any]] = []
        for result in raw.get("results", []) or []:
            url = result.get("url", "")
            content = result.get("raw_content", "") or result.get("content", "")
            documents.append({
                "url": url,
                "title": result.get("title", ""),
                "content": content,
                "raw_content": content,
                "metadata": {"sourceURL": url, "title": result.get("title", "")},
            })
        for fail in raw.get("failed_results", []) or []:
            documents.append({
                "url": fail.get("url", ""),
                "title": "",
                "content": "",
                "error": fail.get("error", "extraction failed"),
            })
        return documents

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Tavily",
            "badge": "paid · search + extract",
            "tag": "Search and page extraction in one API.",
            "env_vars": [
                {"key": "TAVILY_API_KEY", "prompt": "Tavily API key", "url": "https://app.tavily.com/home"},
            ],
        }
