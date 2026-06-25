"""Grok-backed X Search via the xAI Responses API."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.auth.xai_oauth import (
    DEFAULT_XAI_OAUTH_BASE_URL,
    resolve_runtime_credentials,
    validate_xai_oauth_base_url,
)

DEFAULT_X_SEARCH_MODEL = os.getenv("FLOWLY_X_SEARCH_MODEL", "grok-4.20-reasoning")
MAX_X_HANDLES = 20
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_handles(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    values = raw if isinstance(raw, list) else [raw]
    handles: list[str] = []
    seen: set[str] = set()
    for item in values:
        handle = str(item or "").strip().lstrip("@")
        if not handle:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise ValueError(f"Invalid X handle: @{handle}")
        key = handle.lower()
        if key not in seen:
            seen.add(key)
            handles.append(handle)
    if len(handles) > MAX_X_HANDLES:
        raise ValueError(f"At most {MAX_X_HANDLES} X handles are supported")
    return handles


def _validate_date(value: Any, *, field: str) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    parsed = date.fromisoformat(text)
    if field == "from_date" and parsed > date.today():
        raise ValueError("from_date cannot be in the future")
    return text


def _extract_output_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    parts: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
    return "".join(parts).strip()


def _collect_citations(value: Any) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            if node_type in {"url_citation", "citation"} or "url" in node and "title" in node:
                url = node.get("url")
                if isinstance(url, str) and url:
                    citations.append({
                        "url": url,
                        "title": node.get("title") or "",
                        "start_index": node.get("start_index"),
                        "end_index": node.get("end_index"),
                    })
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for citation in citations:
        url = str(citation.get("url") or "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(citation)
    return deduped


async def _resolve_credentials(config: Any | None = None) -> tuple[str, str, str]:
    try:
        creds = await asyncio.to_thread(resolve_runtime_credentials, config=config)
    except Exception as exc:
        logger.debug("x_search OAuth credentials unavailable: {}", exc)
        creds = None
    if creds is not None and creds.api_key:
        return creds.api_key, validate_xai_oauth_base_url(creds.base_url), "xai_oauth"

    api_key = ""
    api_base = DEFAULT_XAI_OAUTH_BASE_URL
    if config is not None:
        provider_cfg = getattr(getattr(config, "providers", None), "xai", None)
        api_key = str(getattr(provider_cfg, "api_key", "") or "").strip()
        api_base = str(getattr(provider_cfg, "api_base", "") or api_base)
    api_key = api_key or os.getenv("XAI_API_KEY", "").strip()
    api_base = os.getenv("XAI_BASE_URL", api_base).strip() or DEFAULT_XAI_OAUTH_BASE_URL
    if not api_key:
        raise ValueError(
            "No xAI credentials available. Run `flowly xai login` for Grok "
            "subscription OAuth or set providers.xai.apiKey / XAI_API_KEY."
        )
    return api_key, validate_xai_oauth_base_url(api_base), "xai"


class XSearchTool(Tool):
    """Search current X posts through xAI's built-in ``x_search`` tool."""

    def __init__(self, config: Any | None = None):
        self._config = config

    @property
    def name(self) -> str:
        return "x_search"

    @property
    def description(self) -> str:
        return (
            "Search X (Twitter) through Grok/xAI. Use for current X posts, "
            "posts from specific handles, and recent X-grounded answers. "
            "This uses xAI OAuth subscription credentials when connected."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The X search or research query.",
                },
                "allowed_x_handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional allowlist of X handles without @.",
                },
                "excluded_x_handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional blocklist of X handles without @.",
                },
                "from_date": {
                    "type": "string",
                    "description": "Optional start date, YYYY-MM-DD.",
                },
                "to_date": {
                    "type": "string",
                    "description": "Optional end date, YYYY-MM-DD.",
                },
                "enable_image_understanding": {
                    "type": "boolean",
                    "description": "Let Grok inspect images in matched posts.",
                },
                "enable_video_understanding": {
                    "type": "boolean",
                    "description": "Let Grok inspect videos in matched posts.",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        allowed_x_handles: Any = None,
        excluded_x_handles: Any = None,
        from_date: str = "",
        to_date: str = "",
        enable_image_understanding: bool = False,
        enable_video_understanding: bool = False,
    ) -> str:
        try:
            clean_query = str(query or "").strip()
            if not clean_query:
                return "Error: query is required"
            allowed = _normalize_handles(allowed_x_handles)
            excluded = _normalize_handles(excluded_x_handles)
            if allowed and excluded:
                return "Error: allowed_x_handles and excluded_x_handles are mutually exclusive"
            start = _validate_date(from_date, field="from_date")
            end = _validate_date(to_date, field="to_date")
            if start and end and start > end:
                return "Error: from_date cannot be after to_date"

            api_key, base_url, source = await _resolve_credentials(self._config)
            tool_config: dict[str, Any] = {"type": "x_search"}
            if allowed:
                tool_config["allowed_x_handles"] = allowed
            if excluded:
                tool_config["excluded_x_handles"] = excluded
            if start:
                tool_config["from_date"] = start
            if end:
                tool_config["to_date"] = end
            if enable_image_understanding:
                tool_config["enable_image_understanding"] = True
            if enable_video_understanding:
                tool_config["enable_video_understanding"] = True

            payload = {
                "model": DEFAULT_X_SEARCH_MODEL,
                "input": [{"role": "user", "content": clean_query}],
                "tools": [tool_config],
                "store": False,
            }
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
                response = await client.post(
                    f"{base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "flowly/x-search",
                    },
                    json=payload,
                )
            if response.status_code == 403 and source == "xai_oauth":
                return (
                    "Error: xAI returned HTTP 403. Your X/Grok subscription is "
                    "authenticated but this API surface is not enabled for the account."
                )
            if response.status_code >= 400:
                return f"Error: xAI X Search HTTP {response.status_code}: {response.text[:500]}"

            data = response.json()
            answer = _extract_output_text(data)
            citations = _collect_citations(data.get("citations")) + _collect_citations(data.get("output"))
            filters_active = bool(allowed or excluded or start or end)
            result = {
                "query": clean_query,
                "answer": answer,
                "citations": citations,
                "degraded": bool(filters_active and not citations),
                "degraded_reason": (
                    "xAI returned no citations for a filtered X search"
                    if filters_active and not citations else ""
                ),
                "source": source,
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        except ValueError as exc:
            return f"Error: {exc}"
        except httpx.HTTPError as exc:
            return f"Error: xAI X Search network failure: {type(exc).__name__}"
        except Exception as exc:
            logger.exception("x_search failed")
            return f"Error: {exc}"
