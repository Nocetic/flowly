"""Exa semantic search + content extraction provider.

Uses the official ``exa-py`` SDK (lazy-imported). Key comes from the
connections card (``tools.web.search.exa.api_key``) or ``EXA_API_KEY``.
Both methods are sync; the web_extract dispatcher wraps sync extract in a
worker thread.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from flowly.agent.tools.web_providers._config import provider_section
from flowly.agent.tools.web_providers.base import WebSearchProvider


def _exa_key() -> str:
    section = provider_section("exa")
    return ((getattr(section, "api_key", "") or "") or os.getenv("EXA_API_KEY", "")).strip()


def _get_client() -> Any:
    key = _exa_key()
    if not key:
        raise ValueError("EXA_API_KEY is not set")
    from exa_py import Exa  # noqa: WPS433 — deliberately lazy

    client = Exa(api_key=key)
    try:
        client.headers["x-exa-integration"] = "flowly"
    except Exception:  # noqa: BLE001 — header tagging is best-effort
        pass
    return client


class ExaWebSearchProvider(WebSearchProvider):
    """Exa neural search + extract."""

    @property
    def name(self) -> str:
        return "exa"

    @property
    def display_name(self) -> str:
        return "Exa"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        section = provider_section("exa")
        if not getattr(section, "enabled", False):
            return False
        return bool(_exa_key())

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        try:
            response = _get_client().search(query, num_results=int(limit), contents={"highlights": True})
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except ImportError as exc:
            return {"success": False, "error": f"Exa SDK not installed: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Exa search error: {}", exc)
            return {"success": False, "error": f"Exa search failed: {exc}"}

        web = []
        for i, result in enumerate(response.results or []):
            highlights = result.highlights or []
            web.append({
                "url": result.url or "",
                "title": result.title or "",
                "description": " ".join(highlights) if highlights else "",
                "position": i + 1,
            })
        return {"success": True, "data": {"web": web}}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        try:
            response = _get_client().get_contents(urls, text=True)
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except ImportError as exc:
            return [{"url": u, "title": "", "content": "", "error": f"Exa SDK not installed: {exc}"} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Exa extract error: {}", exc)
            return [{"url": u, "title": "", "content": "", "error": f"Exa extract failed: {exc}"} for u in urls]

        results: list[dict[str, Any]] = []
        for result in response.results or []:
            content = result.text or ""
            url = result.url or ""
            title = result.title or ""
            results.append({
                "url": url,
                "title": title,
                "content": content,
                "raw_content": content,
                "metadata": {"sourceURL": url, "title": title},
            })
        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Exa",
            "badge": "paid · semantic",
            "tag": "Neural/semantic web search with content extraction.",
            "env_vars": [
                {"key": "EXA_API_KEY", "prompt": "Exa API key", "url": "https://exa.ai"},
            ],
        }
