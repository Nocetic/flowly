"""Local readability extract provider.

Extract-only, no API key, always available — the fallback ``web_extract``
backend when no paid extractor (Firecrawl / Tavily / Exa / Parallel) is
configured. Reuses ``web_fetch``'s fetch + Readability + query-focused
extraction pipeline (``_fetch_readable``).
"""

from __future__ import annotations

from typing import Any

from flowly.agent.tools.web_providers.base import WebSearchProvider


class LocalExtractProvider(WebSearchProvider):
    """Readability-based extract over plain HTTP. Search is not supported."""

    @property
    def name(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        return "Local readability"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        # Imported lazily to avoid a circular import (web.py references the
        # provider registry, which this module belongs to).
        from flowly.agent.tools.web import _fetch_readable, _validate_url

        query = kwargs.get("query")
        max_chars = int(kwargs.get("max_chars") or 50000)

        results: list[dict[str, Any]] = []
        for url in urls:
            ok, reason = _validate_url(str(url))
            if not ok:
                results.append({"url": url, "title": "", "content": "", "error": reason})
                continue
            r = await _fetch_readable(str(url), query=query, extract_mode="markdown", max_chars=max_chars)
            if r.get("error"):
                results.append({"url": url, "title": "", "content": "", "error": r["error"]})
                continue
            final_url = r.get("finalUrl", url)
            text = r.get("text", "")
            results.append(
                {
                    "url": final_url,
                    "title": r.get("title", ""),
                    "content": text,
                    "raw_content": text,
                    "metadata": {"sourceURL": final_url, "title": r.get("title", "")},
                }
            )
        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Local readability",
            "badge": "free · no key · extract only",
            "tag": "Fallback content extraction via Readability — no setup needed.",
            "env_vars": [],
        }
