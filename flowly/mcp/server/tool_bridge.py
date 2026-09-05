"""Authenticated live-runtime transport and a generic MCP stdio front end."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import uuid
from typing import Any
from urllib.parse import urlsplit

from flowly.mcp.server.tool_runtime import (
    MAX_ARGUMENT_BYTES,
    MAX_RESULT_BYTES,
    RuntimeToolBridge,
    ToolBridgeError,
)

_PREFIX = "/api/mcp/tools"
GRANT_ENV = "FLOWLY_TOOL_BRIDGE_GRANT"
_BODY_TIMEOUT = 10
_MAX_HTTP_CALLS = 64


async def _read_json(stream, limit: int) -> dict:
    data = bytearray()
    try:
        async with asyncio.timeout(_BODY_TIMEOUT):
            async for chunk in stream.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise ToolBridgeError("Bridge payload exceeds the size limit")
    except TimeoutError:
        raise ToolBridgeError("Bridge payload timed out") from None
    try:
        body = json.loads(data)
    except (ValueError, RecursionError):
        raise ToolBridgeError("Invalid bridge JSON") from None
    if not isinstance(body, dict):
        raise ToolBridgeError("Bridge payload must be an object")
    return body


def register_tool_bridge_routes(app: Any, bridge: RuntimeToolBridge, *, admin_token: str | None) -> None:
    from aiohttp import web

    body_readers = 0
    active_calls = 0

    def bearer(request):
        header = request.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def local(request):
        try:
            return ipaddress.ip_address(request.remote or "").is_loopback
        except ValueError:
            return False

    async def issue(request):
        supplied = bearer(request)
        if not local(request) or not admin_token or not secrets.compare_digest(supplied, admin_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await _read_json(request.content, 8192)
            key = body.get("session_key")
            # The gateway-admin credential may select an existing conversation;
            # a scoped grant cannot pick another session or issue new grants.
            if not any(row.get("key") == key for row in bridge.owner.sessions.list_sessions()):
                raise ToolBridgeError("Unknown session; use an exact existing conversation key")
            result = bridge.create_grant(
                key, names=body.get("tools"), allow_writes=body.get("allow_writes", False),
                ttl=body.get("ttl_seconds", 3600),
            )
            return web.json_response(result)
        except ToolBridgeError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def authorized(request, action):
        token = bearer(request)
        try:
            if not local(request):
                raise ToolBridgeError("Local tool bridge access is required")
            bridge._grant(token)
        except ToolBridgeError:
            return web.json_response({"error": "invalid or expired tool grant"}, status=401)
        try:
            return await action(token)
        except ToolBridgeError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def listing(request):
        async def run(token):
            return web.json_response({"tools": bridge.list_tools(token)})
        return await authorized(request, run)

    async def revoke(request):
        async def run(token):
            bridge.revoke(token)
            return web.json_response({"revoked": True})
        return await authorized(request, run)

    async def cancel(request):
        async def run(token):
            body = await _read_json(request.content, 8192)
            request_id = body.get("request_id")
            if not isinstance(request_id, str):
                raise ToolBridgeError("request_id is required")
            bridge.cancel(token, request_id)
            return web.json_response({"cancelled": True})
        return await authorized(request, run)

    async def call(request):
        async def invoke(token):
            nonlocal body_readers
            if body_readers >= 4:
                raise ToolBridgeError("Tool bridge payload capacity reached")
            body_readers += 1
            try:
                body = await _read_json(request.content, MAX_ARGUMENT_BYTES + 4096)
            finally:
                body_readers -= 1
            request_id = body.get("request_id")
            invocation = asyncio.create_task(bridge.call(
                token, request_id, body.get("name"), body.get("arguments", {}),
            ))
            transport = request.transport

            async def disconnected():
                while transport is not None and not transport.is_closing():
                    await asyncio.sleep(0.05)

            watcher = asyncio.create_task(disconnected())
            try:
                done, _ = await asyncio.wait({invocation, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if invocation in done and not invocation.cancelled():
                    return web.json_response(await invocation)
                return web.json_response({"error": "Tool call cancelled or grant expired"}, status=409)
            finally:
                if not invocation.done():
                    try:
                        if isinstance(request_id, str):
                            bridge.cancel(token, request_id)
                    except ToolBridgeError:
                        pass
                    invocation.cancel()
                watcher.cancel()
                await asyncio.gather(invocation, watcher, return_exceptions=True)

        async def run(token):
            nonlocal active_calls
            if active_calls >= _MAX_HTTP_CALLS:
                raise ToolBridgeError("Tool bridge HTTP call capacity reached")
            active_calls += 1
            try:
                return await invoke(token)
            finally:
                active_calls -= 1
        return await authorized(request, run)

    async def shutdown(_app):
        await bridge.close()

    app.router.add_post(f"{_PREFIX}/grants", issue)
    app.router.add_delete(f"{_PREFIX}/grant", revoke)
    app.router.add_get(f"{_PREFIX}/list", listing)
    app.router.add_post(f"{_PREFIX}/call", call)
    app.router.add_post(f"{_PREFIX}/cancel", cancel)
    app.on_shutdown.append(shutdown)


def _validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        valid_host = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
        valid = (
            parsed.scheme == "http" and valid_host and parsed.port
            and parsed.path.rstrip("/") == _PREFIX
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ToolBridgeError("Tool bridge endpoint must be a loopback runtime address")
    return endpoint.rstrip("/")


class ToolBridgeClient:
    def __init__(self, endpoint: str, token: str):
        self.endpoint = _validate_endpoint(endpoint)
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise ToolBridgeError("Invalid tool bridge credential")
        self.token = token
        self._session = None

    async def __aenter__(self):
        from aiohttp import ClientSession, ClientTimeout

        self._session = ClientSession(timeout=ClientTimeout(total=650, connect=5), trust_env=False)
        return self

    async def __aexit__(self, *_args):
        await self._session.close()

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            async with self._session.request(
                method, self.endpoint + path, json=body,
                headers={"Authorization": f"Bearer {self.token}"}, allow_redirects=False,
            ) as response:
                payload = await _read_json(response.content, MAX_RESULT_BYTES)
                if response.status >= 300 or "error" in payload:
                    # Only the local trusted bridge's short error is exposed.
                    raise ToolBridgeError(str(payload.get("error") or "Tool bridge request failed")[:1024])
                return payload
        except asyncio.CancelledError:
            raise
        except ToolBridgeError:
            raise
        except Exception:
            raise ToolBridgeError("Live Flowly tool runtime is unavailable") from None

    async def call(self, name: str, arguments: dict) -> dict:
        request_id = uuid.uuid4().hex
        try:
            return await self.request("POST", "/call", {
                "request_id": request_id, "name": name, "arguments": arguments,
            })
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(self.request("POST", "/cancel", {"request_id": request_id}), 1)
            except Exception:
                pass
            raise


class ToolBridgeMCPServer:
    def __init__(self, client: ToolBridgeClient):
        from mcp.server.lowlevel import Server

        from flowly import __version__

        self.client = client
        self.server = Server(
            "flowly-tools", title="Flowly Live Tools", version=__version__,
            description="Session-scoped tools in the running Flowly runtime.",
            instructions=(
                "Use the listed tools with their exact schemas. Permissions are enforced by the live runtime. "
                "Do not repeat a failed write automatically: a lost reply does not prove it did not execute."
            ),
            on_list_tools=self.list_tools, on_call_tool=self.call_tool,
        )

    async def list_tools(self, _context, _params):
        from mcp import types

        result = await self.client.request("GET", "/list")
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(row) for row in result["tools"]], cacheScope="private",
        )

    async def call_tool(self, _context, params):
        from mcp import types

        try:
            return types.CallToolResult.model_validate(await self.client.call(params.name, params.arguments or {}))
        except ToolBridgeError as exc:
            return types.CallToolResult(content=[types.TextContent(text=str(exc))], isError=True)

    async def run(self):
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read, write):
            await self.server.run(read, write, self.server.create_initialization_options())


async def run_tool_bridge(
    *, session_key: str | None = None, allow_writes: bool = False,
    names: list[str] | None = None, ttl: float = 3600,
) -> None:
    raw_grant = os.environ.get(GRANT_ENV)
    if raw_grant:
        if session_key or allow_writes or names:
            raise ToolBridgeError("A supplied grant cannot be widened or rebound by CLI options")
        try:
            grant = json.loads(raw_grant)
            endpoint, token = grant["endpoint"], grant["token"]
        except (ValueError, TypeError, KeyError):
            raise ToolBridgeError("Invalid runtime-supplied tool grant") from None
    else:
        from flowly.mcp.server.writeplane import _control_base

        control = _control_base()
        if control is None:
            raise ToolBridgeError("Flowly gateway is not running; start it before using live tools")
        endpoint = control[0].removesuffix("/control") + _PREFIX
        async with ToolBridgeClient(endpoint, control[1]) as bootstrap:
            grant = await bootstrap.request("POST", "/grants", {
                "session_key": session_key, "allow_writes": allow_writes, "tools": names, "ttl_seconds": ttl,
            })
        token = grant["token"]
    async with ToolBridgeClient(endpoint, token) as client:
        try:
            await ToolBridgeMCPServer(client).run()
        finally:
            try:
                await asyncio.wait_for(client.request("DELETE", "/grant"), 1)
            except Exception:
                pass
