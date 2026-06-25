"""Gateway control endpoint for the MCP write plane (Faz 3c, M1b).

The MCP write tools (send message, list/resolve approvals) need the *live*
gateway process — the outbound bus dispatcher and the in-memory approval
manager only exist there. Rather than couple `flowly mcp serve` to the
gateway's internals, the gateway exposes a tiny authed HTTP control API on
its existing aiohttp app, and `serve`'s write tools call it.

Auth + discovery reuse Flowly's screenshot-delegation pattern: the gateway
writes ``$FLOWLY_HOME/gateway-api.json`` ``{host, port, token}`` (mode 0600)
on start and removes it on stop. The write tools read that file, then send
``Authorization: Bearer <token>`` to the control routes.

This module is split so it's testable without a full gateway:
- :func:`write_api_file` / :func:`read_api_file` / :func:`remove_api_file`
- :func:`register_control_routes` — adds the routes to any aiohttp app.

All routes are **additive and opt-in**: the gateway only registers them
when a control token is provided, so existing gateway behavior is untouched
when the write plane is disabled.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_API_FILENAME = "gateway-api.json"
_CONTROL_PREFIX = "/control"

# Callback the gateway supplies to actually enqueue an outbound message.
SendCallback = Callable[[str, str], Awaitable[bool]]


def _api_path() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / _API_FILENAME


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def write_api_file(host: str, port: int, token: str) -> None:
    """Advertise the control endpoint (mode 0600). Best-effort."""
    path = _api_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
        tmp.write_text(
            json.dumps({"host": host, "port": port, "token": token}),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError as exc:
        logger.warning("MCP control: failed to write %s: %s", _API_FILENAME, exc)


def read_api_file() -> dict[str, Any] | None:
    path = _api_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("token") and data.get("port"):
            return data
    except (OSError, ValueError):
        pass
    return None


def remove_api_file() -> None:
    try:
        _api_path().unlink(missing_ok=True)
    except OSError:
        pass


def register_control_routes(
    app: Any,
    *,
    token: str,
    on_send: SendCallback,
) -> None:
    """Register the authed control routes on an aiohttp application.

    Routes (all under ``/control``, all require ``Authorization: Bearer``):
      - ``POST /control/messages/send``    {target, message}
      - ``GET  /control/approvals``
      - ``POST /control/approvals/resolve`` {id, decision}
    """
    from aiohttp import web

    def _authorized(request: Any) -> bool:
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        # Constant-time compare to avoid token-timing leaks.
        return secrets.compare_digest(header, expected)

    async def _messages_send(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        target = str(body.get("target", "")).strip()
        message = str(body.get("message", ""))
        if not target or not message:
            return web.json_response(
                {"error": "both 'target' and 'message' are required"}, status=400,
            )
        try:
            ok = await on_send(target, message)
        except Exception as exc:
            return web.json_response({"error": f"send failed: {exc}"}, status=500)
        return web.json_response({"sent": bool(ok), "target": target})

    async def _approvals_list(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from flowly.exec.approval_manager import get_approval_manager
        mgr = get_approval_manager()
        items = []
        for p in mgr.list_pending():
            items.append({
                "id": p.id,
                "command": getattr(p.request, "command", ""),
                "session_key": p.session_key,
                "created_at": p.created_at,
                "expires_at": p.expires_at,
                "risk_reasons": list(getattr(p, "risk_reasons", []) or []),
                "supports_always": getattr(p, "supports_always", True),
            })
        return web.json_response({"count": len(items), "approvals": items})

    async def _approvals_resolve(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        approval_id = str(body.get("id", "")).strip()
        decision = str(body.get("decision", "")).strip()
        if decision not in {"allow-once", "allow-always", "deny"}:
            return web.json_response(
                {"error": "decision must be allow-once, allow-always, or deny"},
                status=400,
            )
        if not approval_id:
            return web.json_response({"error": "'id' is required"}, status=400)
        from flowly.exec.approval_manager import get_approval_manager
        ok = get_approval_manager().resolve(approval_id, decision)  # type: ignore[arg-type]
        return web.json_response({"resolved": bool(ok), "id": approval_id})

    app.router.add_post(f"{_CONTROL_PREFIX}/messages/send", _messages_send)
    app.router.add_get(f"{_CONTROL_PREFIX}/approvals", _approvals_list)
    app.router.add_post(f"{_CONTROL_PREFIX}/approvals/resolve", _approvals_resolve)
    logger.info("MCP control routes registered under %s", _CONTROL_PREFIX)
