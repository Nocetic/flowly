"""Web tools: web_search and web_fetch."""

import asyncio
import html
import inspect
import ipaddress
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from flowly.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# SSRF protection — block requests to private/internal networks
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),         # Private class A
    ipaddress.ip_network("172.16.0.0/12"),      # Private class B
    ipaddress.ip_network("192.168.0.0/16"),     # Private class C
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local / AWS IMDS
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

_BLOCKED_HOSTS = frozenset({
    "metadata.google.internal",     # GCP metadata
    "metadata.digitalocean.com",    # DigitalOcean metadata
    "100.100.100.200",              # Alibaba Cloud metadata
})


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate a URL against SSRF threats."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Could not parse URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"Scheme '{parsed.scheme}' not allowed (only http/https)"

    host = parsed.hostname or ""
    if not host:
        return False, "Empty hostname"

    if host in _BLOCKED_HOSTS:
        return False, f"Host '{host}' is blocked (cloud metadata endpoint)"

    if host in ("localhost", "0.0.0.0"):
        return False, "Localhost access is blocked"

    try:
        ip = ipaddress.ip_address(host)
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                return False, f"IP {host} is in blocked private range {network}"
    except ValueError:
        pass

    return True, ""


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# ---------------------------------------------------------------------------
# Query-focused passage extraction
# ---------------------------------------------------------------------------

def _split_into_passages(text: str, min_len: int = 80) -> list[str]:
    """Split text into passages by double newlines or markdown headers."""
    # Split on double newlines, markdown headers, or long single newlines
    raw = re.split(r'\n{2,}|(?=^#{1,6}\s)', text, flags=re.MULTILINE)
    passages = []
    for chunk in raw:
        chunk = chunk.strip()
        if len(chunk) >= min_len:
            passages.append(chunk)
        elif passages:
            # Merge short chunks with previous
            passages[-1] += "\n" + chunk
    return passages or [text]


def _score_passage(passage: str, query_terms: set[str]) -> float:
    """Score a passage by keyword overlap with search query."""
    words = set(re.findall(r'\w{3,}', passage.lower()))
    if not words or not query_terms:
        return 0.0
    overlap = len(words & query_terms)
    # Boost exact phrase matches
    passage_lower = passage.lower()
    phrase_bonus = sum(1.5 for term in query_terms if term in passage_lower)
    return overlap + phrase_bonus


def _extract_relevant_passages(
    text: str,
    query: str,
    max_chars: int = 5000,
) -> str:
    """Extract the most query-relevant passages within a character budget.

    Instead of naively truncating from the start, this scores each
    paragraph/section by keyword overlap with the query and assembles
    the top-scoring passages up to the budget.
    """
    if len(text) <= max_chars:
        return text

    query_terms = set(re.findall(r'\w{3,}', query.lower()))
    passages = _split_into_passages(text)

    # Score each passage
    scored = [(p, _score_passage(p, query_terms)) for p in passages]
    # Sort by score descending, but keep positional order for ties
    scored.sort(key=lambda x: -x[1])

    # Assemble top passages within budget
    selected: list[tuple[int, str]] = []
    remaining = max_chars
    for passage, score in scored:
        if remaining <= 0:
            break
        if len(passage) <= remaining:
            # Track original position for ordering
            idx = passages.index(passage)
            selected.append((idx, passage))
            remaining -= len(passage) + 2  # +2 for separator
        elif remaining > 200:
            # Partial inclusion of high-scoring passage
            idx = passages.index(passage)
            selected.append((idx, passage[:remaining]))
            remaining = 0

    # Re-order by original position for coherent reading
    selected.sort(key=lambda x: x[0])

    result = "\n\n".join(text for _, text in selected)
    if len(result) < len(text):
        result += f"\n\n[... {len(text) - len(result)} chars of lower-relevance content omitted]"
    return result


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool(Tool):
    """Search the web using Brave Search API (direct or via Flowly proxy)."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, snippets, and enriched metadata."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        proxy_url: str | None = None,
        server_id: str | None = None,
        auth_token: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = max_results
        self._proxy_url = proxy_url
        self._server_id = server_id
        self._auth_token = auth_token

        # When the account relay creds are present (a logged-in bot) but no
        # explicit proxy URL was configured, fall back to the canonical Flowly
        # search proxy. The ``proxy_url`` config field is never auto-populated
        # by login — only the relay creds (server_id/auth_token) are — so
        # without this backfill the proxy path silently never activates even
        # though the account is registered. Mirrors the relay_url canonical
        # backfill in ``account/relay_config.py``. (A self-host who set a
        # custom proxy_url keeps it; an own BRAVE_API_KEY still wins in
        # ``execute`` since the direct path is checked first.)
        if not self._proxy_url and self._server_id and self._auth_token:
            base = os.environ.get("FLOWLY_API_BASE", "https://useflowlyapp.com")
            self._proxy_url = base.rstrip("/") + "/api/v1/search"

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        from flowly.agent.tools.web_providers import get_active_search_provider

        provider = get_active_search_provider()
        if provider is None:
            # No plugin-registered backend (e.g. the Codex tool server or a
            # subprocess agent that never booted plugin discovery). Fall back
            # to a Brave provider built from this tool's own resolved creds.
            provider = self._fallback_provider()
        if provider is None:
            return "Error: Web search not available. No API key or proxy configured."

        n = count or self.max_results
        try:
            data = await _run_provider_search(provider, query, n)
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        return _format_search_results(query, data)

    def _fallback_provider(self) -> Any:
        """Build a Brave provider from this tool's own resolved creds."""
        from flowly.agent.tools.web_providers.brave import BraveWebSearchProvider

        if not (
            self.api_key
            or (self._proxy_url and self._server_id and self._auth_token)
        ):
            return None
        return BraveWebSearchProvider(
            api_key=self.api_key,
            proxy_url=self._proxy_url,
            server_id=self._server_id,
            auth_token=self._auth_token,
            max_results=self.max_results,
        )


async def _run_provider_search(provider: Any, query: str, n: int) -> dict:
    """Call a provider's ``search`` — awaiting it or offloading to a thread.

    Search implementations are sync (network I/O), so run them in a worker
    thread to keep the event loop responsive; a provider that declares an
    async ``search`` is awaited directly.
    """
    if inspect.iscoroutinefunction(provider.search):
        return await provider.search(query, n)
    return await asyncio.to_thread(provider.search, query, n)


def _format_search_results(query: str, data: dict) -> str:
    """Render a provider search envelope to the text the agent reads.

    Handles the generic ``{title, url, description, position}`` rows every
    provider returns plus Brave's enrichments (``extra_snippets``, ``age``,
    ``page_age``, ``language``, ``source``, ``meta_url.hostname``) and the
    optional top-level ``summary`` / ``news`` when present.
    """
    if not data.get("success", True):
        return str(data.get("error") or f"No results for: {query}")

    payload = data.get("data", {}) or {}
    web = payload.get("web", []) or []
    if not web:
        return f"No results for: {query}"

    lines = [f"Results for: {query}\n"]
    if summary := payload.get("summary"):
        lines.append(f"Summary: {summary}\n")

    for i, item in enumerate(web, 1):
        line = f"{i}. {item.get('title', '')}\n   {item.get('url', '')}"
        if desc := item.get("description"):
            line += f"\n   {desc}"
        for snippet in item.get("extra_snippets", []) or []:
            line += f"\n   > {snippet}"
        meta_parts = []
        if age := item.get("age"):
            meta_parts.append(age)
        if page_age := item.get("page_age"):
            meta_parts.append(f"published: {page_age}")
        if lang := item.get("language"):
            meta_parts.append(f"lang: {lang}")
        if source := item.get("source"):
            meta_parts.append(source)
        hostname = (item.get("meta_url") or {}).get("hostname", "")
        if hostname:
            meta_parts.append(hostname)
        if meta_parts:
            line += f"\n   [{' · '.join(meta_parts)}]"
        lines.append(line)

    if news := payload.get("news"):
        lines.append("\nRecent News:")
        for item in news:
            source = item.get("source", "")
            age = item.get("age", "")
            lines.append(
                f"- {item.get('title', '')} ({source}, {age})\n  {item.get('url', '')}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# WebFetchTool
# ---------------------------------------------------------------------------

class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability + query-focused extraction."""

    name = "web_fetch"
    description = (
        "Fetch URL and extract readable content (HTML → markdown/text). "
        "Optionally pass a 'query' parameter to get the most relevant passages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "query": {"type": "string", "description": "Search query for relevance-based extraction (recommended)"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars

    async def execute(self, url: str, query: str | None = None, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        from readability import Document

        # SSRF protection
        allowed, reason = _validate_url(url)
        if not allowed:
            return json.dumps({"error": reason, "url": url})

        max_chars = maxChars or self.max_chars

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0)
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            # Query-focused extraction: if a query is provided, extract the
            # most relevant passages instead of naively head-truncating.
            original_length = len(text)
            if query and len(text) > max_chars:
                text = _extract_relevant_passages(text, query, max_chars)
                extractor += "+relevance"
            elif len(text) > max_chars:
                text = text[:max_chars]

            truncated = len(text) < original_length

            return json.dumps({
                "url": url,
                "finalUrl": str(r.url),
                "status": r.status_code,
                "extractor": extractor,
                "truncated": truncated,
                "length": len(text),
                "originalLength": original_length,
                "text": text,
            })
        except Exception as e:
            return json.dumps({"error": str(e), "url": url})

    def _to_markdown(self, html_content: str) -> str:
        """Convert HTML to markdown."""
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html_content, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
