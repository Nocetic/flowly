"""Native MCP Streamable HTTP on the owning gateway, with scoped credentials.

The SDK owns protocol parsing and negotiation. The small HTTP/ASGI adapter is
bounded and stateless: clients reconnect without sharing another client's MCP
session. Remote plaintext and browser-origin requests are rejected, regardless
of forwarded headers. The gateway administrator token is never accepted here.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

from flowly.mcp.external_access import ExternalAccessError
from flowly.mcp.server.tool_runtime import MAX_ARGUMENT_BYTES, MAX_RESULT_BYTES, ToolBridgeError


def register_external_mcp_route(app, service) -> None:
    from aiohttp import web
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    active = 0

    def safe_transport(request):
        try:
            host = urlsplit("//" + request.host).hostname
            loopback = ipaddress.ip_address(request.remote or "").is_loopback and (
                host == "localhost" or ipaddress.ip_address(host or "").is_loopback
            )
        except ValueError:
            loopback = False
        return (request.secure or loopback) and "Origin" not in request.headers and not request.query_string

    async def handle(request):
        nonlocal active
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if not safe_transport(request):
            return web.json_response({"error": "MCP requires HTTPS, or a local loopback connection"}, status=403, headers=headers)
        auth = request.headers.getall("Authorization", [])
        token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
        try:
            service.authorize(token)
        except ExternalAccessError:
            return web.json_response({"error": "Invalid, revoked or expired MCP access key"}, status=401,
                                     headers={**headers, "WWW-Authenticate": "Bearer"})
        if active >= 32:
            return web.json_response({"error": "MCP request capacity reached"}, status=429, headers=headers)
        active += 1
        try:
            return await dispatch(request, token, headers)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, TimeoutError):
            return web.json_response({"error": "MCP request timed out or disconnected"}, status=408, headers=headers)
        except (ExternalAccessError, ToolBridgeError):
            return web.json_response({"error": "MCP request is no longer authorized"}, status=403, headers=headers)
        except Exception:
            return web.json_response({"error": "MCP request failed; check the selected runtime"}, status=500, headers=headers)
        finally:
            active -= 1

    async def dispatch(request, token, headers):
        body = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_ARGUMENT_BYTES + 4096:
                    return web.json_response({"error": "MCP request is too large"}, status=413, headers=headers)
        service.authorize(token)

        async def list_tools(_context, _params):
            return types.ListToolsResult(
                tools=[types.Tool.model_validate(row) for row in await service.list_tools(token)], cacheScope="private",
            )

        async def call_tool(_context, params):
            try:
                return types.CallToolResult.model_validate(await service.call(token, params.name, params.arguments or {}))
            except (ExternalAccessError, ToolBridgeError) as exc:
                return types.CallToolResult(content=[types.TextContent(text=str(exc))], isError=True)

        server = Server(
            "flowly", title="Flowly", on_list_tools=list_tools, on_call_tool=call_tool,
            instructions="Only explicitly allowed tools are listed. Do not retry an uncertain write automatically.",
        )
        manager = StreamableHTTPSessionManager(
            server, stateless=True, json_response=True,
            # Equivalent checks live above: HTTPS/loopback, bearer and no browser
            # Origin. Do not derive trusted forwarding hosts from a request.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            max_request_body_size=MAX_ARGUMENT_BYTES + 4096,
        )
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": request.method, "scheme": request.scheme,
            "path": "/mcp", "raw_path": b"/mcp", "query_string": b"", "root_path": "",
            "headers": [(key.lower(), value) for key, value in request.raw_headers if key.lower() != b"authorization"],
            "server": (request.host, 443 if request.secure else 80), "client": (request.remote or "", 0),
        }
        received = False
        status = 500
        response_headers = dict(headers)
        response_body = bytearray()

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await asyncio.Future()

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers.update({k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])})
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                if len(response_body) > MAX_RESULT_BYTES:
                    raise ExternalAccessError("MCP response exceeded the size limit")

        async def run():
            async with manager.run():
                await manager.handle_request(scope, receive, send)

        async def disconnected():
            while request.transport is not None and not request.transport.is_closing():
                await asyncio.sleep(0.1)

        task = asyncio.create_task(run())
        watcher = asyncio.create_task(disconnected())
        try:
            async with asyncio.timeout(620):
                done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if task not in done:
                    raise ConnectionError
                await task
                return web.Response(status=status, headers=response_headers, body=bytes(response_body))
        finally:
            task.cancel()
            watcher.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)

    async def shutdown(_app):
        await service.close()

    app.router.add_route("*", "/mcp", handle)
    app.on_shutdown.append(shutdown)
