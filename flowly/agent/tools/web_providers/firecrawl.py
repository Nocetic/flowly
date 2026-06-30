"""Firecrawl search + content extraction provider.

The strongest extractor — JS rendering, anti-bot handling. Direct cloud key
(``FIRECRAWL_API_KEY``) or a self-hosted instance (``FIRECRAWL_API_URL``),
read from the connections card or the env. The SDK is lazy-imported.

Async ``extract``: each URL is scraped in a worker thread with a 60s timeout,
and the post-redirect final URL is re-checked against Flowly's SSRF guard.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger

from flowly.agent.tools.web_providers._config import provider_section
from flowly.agent.tools.web_providers.base import WebSearchProvider


def _firecrawl_config() -> tuple[str, str]:
    section = provider_section("firecrawl")
    api_key = ((getattr(section, "api_key", "") or "") or os.getenv("FIRECRAWL_API_KEY", "")).strip()
    api_url = ((getattr(section, "api_url", "") or "") or os.getenv("FIRECRAWL_API_URL", "")).strip().rstrip("/")
    return api_key, api_url


def _get_client() -> Any:
    api_key, api_url = _firecrawl_config()
    if not api_key and not api_url:
        raise ValueError("FIRECRAWL_API_KEY (cloud) or FIRECRAWL_API_URL (self-hosted) is not set")
    from firecrawl import Firecrawl  # noqa: WPS433 — deliberately lazy

    kwargs: dict[str, str] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if api_url:
        kwargs["api_url"] = api_url
    return Firecrawl(**kwargs)


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        try:
            return {k: v for k, v in value.__dict__.items() if not k.startswith("_")}
        except Exception:  # noqa: BLE001
            pass
    return value


def _extract_search_results(response: Any) -> list[dict[str, Any]]:
    rp = _to_plain(response)
    if isinstance(rp, dict):
        data = rp.get("data")
        if isinstance(data, list):
            return [p for p in (_to_plain(x) for x in data) if isinstance(p, dict)]
        if isinstance(data, dict):
            for k in ("web", "results"):
                v = data.get(k)
                if isinstance(v, list):
                    return [p for p in (_to_plain(x) for x in v) if isinstance(p, dict)]
        for k in ("web", "results"):
            v = rp.get(k)
            if isinstance(v, list):
                return [p for p in (_to_plain(x) for x in v) if isinstance(p, dict)]
    return []


class FirecrawlWebSearchProvider(WebSearchProvider):
    """Firecrawl search + extract (direct cloud or self-hosted)."""

    @property
    def name(self) -> str:
        return "firecrawl"

    @property
    def display_name(self) -> str:
        return "Firecrawl"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        section = provider_section("firecrawl")
        if not getattr(section, "enabled", False):
            return False
        api_key, api_url = _firecrawl_config()
        return bool(api_key or api_url)

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        try:
            response = _get_client().search(query=query, limit=int(limit))
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except ImportError as exc:
            return {"success": False, "error": f"Firecrawl SDK not installed: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Firecrawl search error: {}", exc)
            return {"success": False, "error": f"Firecrawl search failed: {exc}"}

        web = []
        for i, r in enumerate(_extract_search_results(response)):
            web.append({
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("description", "") or r.get("snippet", "")),
                "position": i + 1,
            })
        return {"success": True, "data": {"web": web}}

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        from flowly.agent.tools.web import _validate_url

        fmt = kwargs.get("format")
        formats = ["markdown"] if fmt == "markdown" else (["html"] if fmt == "html" else ["markdown", "html"])

        try:
            client = _get_client()
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except ImportError as exc:
            return [{"url": u, "title": "", "content": "", "error": f"Firecrawl SDK not installed: {exc}"} for u in urls]

        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                try:
                    scrape = await asyncio.wait_for(
                        asyncio.to_thread(client.scrape, url=url, formats=formats),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    results.append({"url": url, "title": "", "content": "", "error": "Firecrawl scrape timed out after 60s"})
                    continue

                payload = _to_plain(scrape)
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    payload = payload["data"]
                if not isinstance(payload, dict):
                    payload = {}

                metadata = _to_plain(payload.get("metadata", {})) or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                title = metadata.get("title", "")
                final_url = metadata.get("sourceURL", url)

                # Re-check SSRF after any redirect.
                ok, reason = _validate_url(str(final_url))
                if not ok:
                    results.append({"url": final_url, "title": title, "content": "", "error": reason})
                    continue

                content = payload.get("markdown") or payload.get("html") or ""
                results.append({
                    "url": final_url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": metadata,
                })
            except Exception as exc:  # noqa: BLE001
                logger.debug("Firecrawl scrape failed for {}: {}", url, exc)
                results.append({"url": url, "title": "", "content": "", "error": str(exc)})
        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Firecrawl",
            "badge": "paid · best extract",
            "tag": "Full search + JS-rendered extraction; cloud key or self-hosted URL.",
            "env_vars": [
                {"key": "FIRECRAWL_API_KEY", "prompt": "Firecrawl API key (blank for self-hosted)", "url": "https://docs.firecrawl.dev/introduction"},
            ],
        }
