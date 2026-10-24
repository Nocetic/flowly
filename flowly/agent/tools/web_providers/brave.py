"""Brave Search provider — Flowly's default web search backend.

Search-only. Three auth paths, checked in this order:

1. **Direct** — a Brave Search API key (``tools.web.search.apiKey`` or the
   ``BRAVE_API_KEY`` env var). Used by self-hosters with their own key.
2. **Flowly proxy, relay creds** — ``channels.web.serverId`` + ``authToken``
   (a relay-registered bot) POST to the Flowly search proxy.
3. **Flowly proxy, account key** — the ``flw_…`` key in
   ``providers.flowly.accountKey``, the same bearer the hosted model provider
   uses. A Mac signed in through the app and a Flowly cloud computer have it
   but no relay creds, so without this path their search was "not available"
   while their chat worked. Relay creds stay first while both exist, so a
   proxy that does not take the account key yet keeps working.

``proxy_url`` is backfilled to the canonical endpoint when either proxy
credential is present but no explicit proxy URL was configured.

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
        account_key: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._proxy_url = proxy_url
        self._server_id = server_id
        self._auth_token = auth_token
        self._max_results = max_results
        self._account_key = account_key

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

        if not proxy_url and ((server_id and auth_token) or self._resolve_account_key()):
            base = os.environ.get("FLOWLY_API_BASE", "https://useflowlyapp.com")
            proxy_url = base.rstrip("/") + "/api/v1/search"

        return api_key, proxy_url, server_id, auth_token

    def _resolve_account_key(self) -> str:
        """The Flowly account key (``flw_…``) for the proxy, or ``""``.

        An explicit value wins. A provider built with explicit relay creds and
        no account key (the tool's fallback, older callers) has none; one built
        with nothing explicit reads ``providers.flowly.accountKey``.
        """
        if self._account_key is not None:
            return self._account_key.strip()
        if any(v is not None for v in (self._api_key, self._proxy_url, self._server_id, self._auth_token)):
            return ""
        flowly = getattr(getattr(_cfg(), "providers", None), "flowly", None)
        key = (getattr(flowly, "account_key", "") or "").strip()
        return key if key.startswith("flw_") else ""

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
        return bool(api_key) or bool(proxy_url and ((server_id and auth_token) or self._resolve_account_key()))

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
        account_key = self._resolve_account_key()
        if proxy_url and account_key:
            return self._search_proxy(query, n, proxy_url, account_key=account_key)
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
        query: str, n: int, proxy_url: str, server_id: str = "", auth_token: str = "",
        account_key: str = "",
    ) -> dict:
        # Relay creds go as the two headers; an account key as the bearer the
        # model proxy takes too (``Authorization: Bearer flw_…``).
        if account_key:
            headers = {"Authorization": f"Bearer {account_key}", "Content-Type": "application/json"}
        else:
            headers = {
                "X-Flowly-Server-Id": server_id,
                "X-Flowly-API-Key": auth_token,
                "Content-Type": "application/json",
            }
        try:
            r = httpx.post(
                proxy_url,
                json={"query": query, "count": n},
                headers=headers,
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
            "tag": "Direct Brave API key, or the Flowly search proxy when signed in to a Flowly account.",
            "env_vars": [
                {
                    "key": "BRAVE_API_KEY",
                    "prompt": "Brave Search API key",
                    "url": "https://brave.com/search/api/",
                },
            ],
        }
