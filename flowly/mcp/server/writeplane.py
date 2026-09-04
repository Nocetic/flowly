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

import ipaddress
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_MAX_CONTROL_RESPONSE_BYTES = 4 * 1024 * 1024


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
    host = str(host).strip()
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    try:
        is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        logger.warning("MCP control advertisement rejected non-loopback host")
        return None
    url_host = f"[{host}]" if ":" in host else host
    return f"http://{url_host}:{port}/control", str(token)


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

    import urllib.error
    import urllib.request

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
            raw = resp.read(_MAX_CONTROL_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_CONTROL_RESPONSE_BYTES:
                return {"error": "control response exceeded the size limit"}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"error": "invalid control response"}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(_MAX_CONTROL_RESPONSE_BYTES + 1)
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {
                "error": f"control endpoint returned HTTP {exc.code}",
            }
        except Exception:
            return {"error": f"control endpoint returned HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {
            "error": "Flowly gateway is not reachable "
                     f"({exc.reason}). Is `flowly gateway` running?",
        }
    except Exception as exc:
        return {"error": f"control request failed: {exc}"}


def register_write_tools(mcp: Any, render: Callable[[dict[str, Any]], Any]) -> None:
    """Register the gateway-backed write tools on the MCP server."""
    from mcp import types as mcp_types

    send_annotations = mcp_types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    approval_read_annotations = mcp_types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    approval_write_annotations = mcp_types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )

    @mcp.tool(annotations=send_annotations, structured_output=False)
    def messages_send(
        target: str,
        message: str,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send a message to a channel conversation.

        Requires the Flowly gateway to be running.

        Args:
            target: 'channel:chat_id' (e.g. 'telegram:123456789')
            message: the text to send
            idempotency_key: optional unique key that makes retries safe
        """
        payload = {"target": target, "message": message}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return render(_request("POST", "/messages/send", payload))

    @mcp.tool(annotations=approval_read_annotations, structured_output=False)
    def approvals_list() -> Any:
        """List pending exec approval requests (requires a running gateway)."""
        return render(_request("GET", "/approvals"))

    @mcp.tool(annotations=approval_write_annotations, structured_output=False)
    def approvals_resolve(id: str, decision: str) -> Any:
        """Resolve a pending approval (requires a running gateway).

        Args:
            id: the approval id from approvals_list
            decision: one of 'allow-once', 'allow-always', 'deny'
        """
        return render(_request("POST", "/approvals/resolve",
                               {"id": id, "decision": decision}))
