"""Brave Search provider — Flowly's default web search backend.

Search-only. Two auth paths, checked in this order:

1. **Direct** — a Brave Search API key (``tools.web.search.apiKey`` or the
   ``BRAVE_API_KEY`` env var). Used by self-hosters with their own key.
2. **Flowly proxy** — the account relay creds (``channels.web.serverId`` +
   ``authToken``, written by ``flowly login``) POST to the Flowly Cloud
   search proxy. ``proxy_url`` is backfilled to the canonical endpoint when
   the account is registered but no explicit proxy URL was configured.

Returns the standard provider envelope plus Brave's enrichments
(``extra_snippets``, ``age``, ``source``, top-level ``summary`` / ``news``),
which the ``web_search`` formatter renders when present.
"""

from __future__ import annotations

import os

import httpx
from loguru import logger

from flowly.agent.tools.web_providers.base import WebSearchProvider

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _cfg() -> object | None:
    """Return the ``tools.web.search`` config block, or None if unreadable."""
    try:
        from flowly.config.loader import load_config

        return load_config()
    except Exception:
        return None


class BraveWebSearchProvider(WebSearchProvider):
    """Brave Search via direct API key or the Flowly Cloud proxy.

    Credentials default to config + env, but may be passed explicitly so the
    ``web_search`` tool can build a fallback provider from its own resolved
    creds in contexts without plugin discovery (the Codex tool server,
    subprocess agents). Explicit values (including empty strings) are used
    verbatim; ``None`` means "resolve from config/env".
    """

    def __init__(
        self,
        api_key: str | None = None,
        proxy_url: str | None = None,
        server_id: str | None = None,
        auth_token: str | None = None,
        max_results: int = 5,
    ) -> None:
        self._api_key = api_key
        self._proxy_url = proxy_url
        self._server_id = server_id
        self._auth_token = auth_token
        self._max_results = max_results

    @property
    def name(self) -> str:
        return "brave"

    @property
    def display_name(self) -> str:
        return "Brave Search"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    # -- credential resolution -------------------------------------------

    def _resolve_creds(self) -> tuple[str, str, str, str]:
        """Resolve (api_key, proxy_url, server_id, auth_token).

        Explicitly-passed values win; otherwise read config + env. Applies
        the canonical-proxy backfill (logged-in account but no proxy_url).
        """
        cfg = _cfg()
        search = getattr(getattr(cfg, "tools", None), "web", None)
        search = getattr(search, "search", None)
        chan = getattr(getattr(cfg, "channels", None), "web", None)

        api_key = self._api_key
        if api_key is None:
            api_key = getattr(search, "api_key", "") or os.environ.get("BRAVE_API_KEY", "")
        proxy_url = self._proxy_url
        if proxy_url is None:
            proxy_url = getattr(search, "proxy_url", "") or ""
        server_id = self._server_id
        if server_id is None:
            server_id = getattr(chan, "server_id", "") or ""
        auth_token = self._auth_token
        if auth_token is None:
            auth_token = getattr(chan, "auth_token", "") or ""

        if not proxy_url and server_id and auth_token:
            base = os.environ.get("FLOWLY_API_BASE", "https://useflowlyapp.com")
            proxy_url = base.rstrip("/") + "/api/v1/search"

        return api_key, proxy_url, server_id, auth_token

    def _config_enabled(self) -> bool:
        """Honour the connections-card toggle (``tools.web.search.enabled``).

        Defaults to True so installs without the key keep working. Only the
        config-backed flag is consulted; explicitly-constructed providers
        (the tool's fallback path) still respect a user's global toggle.
        """
        cfg = _cfg()
        search = getattr(getattr(getattr(cfg, "tools", None), "web", None), "search", None)
        val = getattr(search, "enabled", True)
        return True if val is None else bool(val)

    def is_available(self) -> bool:
        if not self._config_enabled():
            return False
        api_key, proxy_url, server_id, auth_token = self._resolve_creds()
        return bool(api_key) or bool(proxy_url and server_id and auth_token)

    # -- search ----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> dict:
        api_key, proxy_url, server_id, auth_token = self._resolve_creds()
        try:
            n = min(max(int(limit or self._max_results), 1), 10)
        except (TypeError, ValueError):
            n = self._max_results

        if api_key:
            return self._search_direct(query, n, api_key)
        if proxy_url and server_id and auth_token:
            return self._search_proxy(query, n, proxy_url, server_id, auth_token)
        return {
            "success": False,
            "error": "Error: Web search not available. No API key or proxy configured.",
        }

    @staticmethod
    def _search_direct(query: str, n: int, api_key: str) -> dict:
        try:
            r = httpx.get(
                _BRAVE_ENDPOINT,
                params={
                    "q": query,
                    "count": n,
                    "extra_snippets": "true",
                    "text_decorations": "false",
                },
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                timeout=10.0,
            )
            r.raise_for_status()
            results = (r.json().get("web") or {}).get("results", []) or []
            web = [{**item, "position": i + 1} for i, item in enumerate(results[:n])]
            return {"success": True, "data": {"web": web}}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Brave direct search error: {}", exc)
            return {"success": False, "error": f"Error: {exc}"}

    @staticmethod
    def _search_proxy(
        query: str, n: int, proxy_url: str, server_id: str, auth_token: str
    ) -> dict:
        try:
            r = httpx.post(
                proxy_url,
                json={"query": query, "count": n},
                headers={
                    "X-Flowly-Server-Id": server_id,
                    "X-Flowly-API-Key": auth_token,
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
            if r.status_code == 429:
                msg = r.json().get("error", {}).get("message", "Try again later")
                return {"success": False, "error": f"Search rate limit reached: {msg}"}
            r.raise_for_status()
            data = r.json()
            results = data.get("results", []) or []
            web = [{**item, "position": i + 1} for i, item in enumerate(results[:n])]
            payload: dict = {"web": web}
            if summary := data.get("summary"):
                payload["summary"] = summary
            if news := data.get("news"):
                payload["news"] = news
            return {"success": True, "data": payload}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Brave proxy search error: {}", exc)
            return {"success": False, "error": f"Error: {exc}"}

    def get_setup_schema(self) -> dict:
        return {
            "name": "Brave Search",
            "badge": "default",
            "tag": "Direct Brave API key, or the Flowly Cloud search proxy when logged in.",
            "env_vars": [
                {
                    "key": "BRAVE_API_KEY",
                    "prompt": "Brave Search API key",
                    "url": "https://brave.com/search/api/",
                },
            ],
        }
