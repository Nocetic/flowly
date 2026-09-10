"""Token-authenticated MCP management over TLS or an SSH loopback forward.

This deliberately does not change the gateway's ordinary chat transport.
Forwarded headers never establish trust: only the socket peer and TLS do.
"""

import ipaddress
import json

from aiohttp import web

from flowly.channels import feature_rpc
from flowly.gateway.auth import extract_request_token, host_origin_allowed, token_matches

METHODS = frozenset({
    "mcp.capabilities", "mcp.connections.list", "mcp.connections.action",
    "mcp.setup.begin", "mcp.setup.status", "mcp.setup.pending",
    "mcp.setup.confirm", "mcp.setup.callback", "mcp.setup.cancel",
    "mcp.setup.cancel_request", "mcp.access.catalog", "mcp.access.list",
    "mcp.access.create", "mcp.access.revoke", "mcp.chat.pending", "mcp.chat.cancel",
})
MAX_BODY = 256 * 1024


def _error(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status,
                             headers={"Cache-Control": "no-store"})


def _protected_socket(request: web.Request) -> bool:
    if request.secure:
        return True
    peer = request.transport.get_extra_info("peername") if request.transport else None
    try:
        address = ipaddress.ip_address(peer[0])
        return address.is_loopback or bool(
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped and address.ipv4_mapped.is_loopback
        )
    except (TypeError, ValueError, IndexError):
        return False


def register_mcp_management(app: web.Application, gateway) -> None:
    _register_management(app, gateway, methods=METHODS, path="/api/mcp/manage", surface="MCP")


def register_gmail_management(app: web.Application, gateway) -> None:
    from flowly.integrations.gmail_rpc import METHODS as GMAIL_METHODS
    _register_management(app, gateway, methods=GMAIL_METHODS, path="/api/gmail/manage", surface="Gmail")


def _register_management(app, gateway, *, methods: frozenset[str], path: str, surface: str) -> None:
    async def handle(request: web.Request) -> web.Response:
        # Loopback is a transport boundary, never an authentication bypass.
        if not token_matches(extract_request_token(request), gateway._auth_token):
            return _error("UNAUTHORIZED", "A valid gateway token is required.", 401)
        if not _protected_socket(request) or not host_origin_allowed(request):
            return _error("SECURE_TRANSPORT_REQUIRED", "Use TLS or an SSH tunnel.", 403)
        try:
            body = bytearray()
            async for chunk in request.content.iter_chunked(16 * 1024):
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return _error("TOO_LARGE", f"{surface} request is too large.", 413)
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return _error("INVALID_PARAMS", f"Expected a JSON {surface} request.")
        if not isinstance(payload, dict) or set(payload) - {"method", "params", "profile"}:
            return _error("INVALID_PARAMS", f"Invalid {surface} request envelope.")
        method, params, profile = payload.get("method"), payload.get("params", {}), payload.get("profile")
        if not isinstance(method, str) or method not in methods:
            return _error("UNKNOWN_METHOD", f"Only {surface} management methods are supported.")
        if not isinstance(params, dict) or (profile is not None and (
            not isinstance(profile, str) or not profile.strip() or len(profile) > 128
        )):
            return _error("INVALID_PARAMS", f"Invalid {surface} request parameters.")
        try:
            if profile is not None:
                if gateway._profile_host is None:
                    return _error("PROFILE_HOST_UNAVAILABLE", "This gateway does not manage profiles.", 503)
                result = await gateway._profile_host.dispatch("profiles.rpc", {
                    "name": profile, "method": method, "params": params,
                })
            else:
                result, _ = await feature_rpc.dispatch(method, params)
        except feature_rpc.FeatureRpcError as exc:
            return _error(exc.code, exc.message)
        except Exception:
            # Credentials and OAuth callbacks must never escape via diagnostics.
            return _error("UNAVAILABLE", f"{surface} management could not complete the request.", 503)
        return web.json_response({"result": result}, headers={"Cache-Control": "no-store"})

    app.router.add_post(path, handle)
