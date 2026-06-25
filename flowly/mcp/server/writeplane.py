"""Write-plane MCP tools for `flowly mcp serve --allow-writes` (Faz 3c).

Send and approval-resolve need the live gateway (outbound dispatcher +
in-memory approval manager), so these tools are thin HTTP clients to the
gateway's control endpoint (see :mod:`flowly.mcp.server.control`). They
discover it via ``$FLOWLY_HOME/gateway-api.json`` and authenticate with its
token.

Every tool **degrades gracefully**: if the gateway isn't running (no
api file / connection refused), it returns a clear error envelope instead
of raising, so the MCP client sees "gateway not running" rather than a
transport crash.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0


def _control_base() -> tuple[str, str] | None:
    """Return ``(base_url, token)`` for the control endpoint, or None."""
    from flowly.mcp.server.control import read_api_file
    info = read_api_file()
    if not info:
        return None
    host = info.get("host") or "127.0.0.1"
    port = info.get("port")
    token = info.get("token")
    if not port or not token:
        return None
    return f"http://{host}:{port}/control", str(token)


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    """Make an authed control request; return parsed JSON or an error dict."""
    base = _control_base()
    if base is None:
        return {
            "error": "Flowly gateway is not running (no gateway-api.json). "
                     "Start it with `flowly gateway` to enable write tools.",
        }
    base_url, token = base
    url = f"{base_url}{path}"

    import urllib.request
    import urllib.error

    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except Exception:
            return {"error": f"control endpoint returned HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {
            "error": "Flowly gateway is not reachable "
                     f"({exc.reason}). Is `flowly gateway` running?",
        }
    except Exception as exc:
        return {"error": f"control request failed: {exc}"}


def register_write_tools(mcp: Any, dumps: Callable[[Any], str]) -> None:
    """Register the gateway-backed write tools on the FastMCP server."""

    @mcp.tool()
    def messages_send(target: str, message: str) -> str:
        """Send a message to a channel conversation.

        Requires the Flowly gateway to be running.

        Args:
            target: 'channel:chat_id' (e.g. 'telegram:123456789')
            message: the text to send
        """
        return dumps(_request("POST", "/messages/send",
                              {"target": target, "message": message}))

    @mcp.tool()
    def approvals_list() -> str:
        """List pending exec approval requests (requires a running gateway)."""
        return dumps(_request("GET", "/approvals"))

    @mcp.tool()
    def approvals_resolve(id: str, decision: str) -> str:
        """Resolve a pending approval (requires a running gateway).

        Args:
            id: the approval id from approvals_list
            decision: one of 'allow-once', 'allow-always', 'deny'
        """
        return dumps(_request("POST", "/approvals/resolve",
                              {"id": id, "decision": decision}))
