"""WebSearchTool proxy-URL resolution.

The search proxy needs three things to fire: ``server_id`` + ``auth_token``
(written into ``channels.web`` by ``flowly login``) AND a ``proxy_url``. The
``proxy_url`` config field is never auto-populated by any flow — only the relay
creds are — so a logged-in bot with the default empty ``proxy_url`` would
silently never use the proxy. WebSearchTool backfills the canonical Flowly
search endpoint in that case (mirrors the relay_url backfill in
``account/relay_config.py``).
"""

import os
from unittest import mock

from flowly.agent.tools.web import WebSearchTool


def test_proxy_url_backfilled_when_logged_in_but_unset():
    """server_id + auth_token present, proxy_url empty → canonical fallback."""
    tool = WebSearchTool(server_id="srv_1", auth_token="tok_1", proxy_url="")
    assert tool._proxy_url == "https://useflowlyapp.com/api/v1/search"


def test_proxy_url_backfilled_when_none():
    tool = WebSearchTool(server_id="srv_1", auth_token="tok_1", proxy_url=None)
    assert tool._proxy_url == "https://useflowlyapp.com/api/v1/search"


def test_explicit_proxy_url_is_kept():
    """A user/self-host who set a custom proxy_url keeps it (no override)."""
    tool = WebSearchTool(
        server_id="srv_1", auth_token="tok_1",
        proxy_url="https://my-relay.example/api/v1/search",
    )
    assert tool._proxy_url == "https://my-relay.example/api/v1/search"


def test_no_backfill_without_auth_token():
    """server_id alone (no auth_token) is not a registered account → no proxy."""
    tool = WebSearchTool(server_id="srv_1", auth_token=None, proxy_url="")
    assert not tool._proxy_url


def test_no_backfill_when_not_logged_in():
    """No relay creds at all → proxy stays unset (direct/own-key path only)."""
    tool = WebSearchTool(proxy_url="")
    assert not tool._proxy_url


def test_backfill_respects_flowly_api_base_override():
    """Local-dev override of FLOWLY_API_BASE flows into the proxy fallback."""
    with mock.patch.dict(os.environ, {"FLOWLY_API_BASE": "http://localhost:3000"}):
        tool = WebSearchTool(server_id="srv_1", auth_token="tok_1", proxy_url="")
    assert tool._proxy_url == "http://localhost:3000/api/v1/search"


def test_own_brave_key_still_takes_precedence_at_runtime():
    """Backfill happens, but a configured api_key wins in execute() routing."""
    tool = WebSearchTool(
        api_key="brave_key", server_id="srv_1", auth_token="tok_1", proxy_url="",
    )
    # proxy is backfilled (harmless) ...
    assert tool._proxy_url == "https://useflowlyapp.com/api/v1/search"
    # ... but the direct path is selected because api_key is set.
    assert tool.api_key == "brave_key"
