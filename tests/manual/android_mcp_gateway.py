"""Loopback-only Android transport fixture with the real owner RPC/MCP runtime.

Run as a module from this worktree, then adb reverse its port. This is not a
production gateway or a relay substitute. It never reads the owner's config:
each run gets a temporary FLOWLY_HOME and a harmless child MCP process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile


async def stdio_server() -> None:
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server as transport

    async def listing(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(
            name=name, description="Isolated Android test " + name,
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(readOnlyHint=name == "read_fixture"),
        ) for name in ("read_fixture", "write_fixture")])

    async def call(ctx, params):
        return types.CallToolResult(content=[types.TextContent(text="fixture only")])

    server = Server("android-fixture", on_list_tools=listing, on_call_tool=call)
    async with transport() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def serve(port: int) -> None:
    from aiohttp import web, WSMsgType

    with tempfile.TemporaryDirectory(prefix="flowly-android-mcp-gateway-") as directory:
        os.environ["FLOWLY_HOME"] = directory
        from flowly.agent.tools.registry import ToolRegistry
        from flowly.channels import feature_rpc
        from flowly.mcp.client import shutdown_mcp_servers
        from flowly.mcp.connections import MCPConnectionService

        path = Path(directory) / "config.json"
        path.write_text(json.dumps({"mcpServers": {"android_fixture": {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--stdio"],
            "enabled": True, "osvCheck": False,
            "tools": {"mode": "none", "resources": False, "prompts": False},
        }}}))
        registry = ToolRegistry()
        service = MCPConnectionService(path, lambda: registry)
        feature_rpc._mcp_connection_service = service
        methods = [name for name in feature_rpc._DISPATCH if name.startswith("mcp.")
                   and name not in {"mcp.list", "mcp.upsert", "mcp.remove", "mcp.oauth"}
                   and not name.startswith("mcp.access.")]
        token = secrets.token_urlsafe(32)
        tickets: set[str] = set()
        sockets: set[web.WebSocketResponse] = set()
        receipts: list[dict] = []
        drop_next: str | None = None

        def authorize(request):
            if not secrets.compare_digest(request.headers.get("X-Flowly-Token", ""), token):
                raise web.HTTPUnauthorized()

        async def ticket(request):
            authorize(request)
            value = secrets.token_urlsafe(32)
            tickets.add(value)
            return web.json_response({"ticket": value})

        async def control(request):
            nonlocal drop_next
            authorize(request)
            if request.method == "POST":
                body = await request.json()
                if body.get("dropNext") not in {None, "mcp.setup.begin", "mcp.setup.confirm"}:
                    raise web.HTTPBadRequest()
                drop_next = body.get("dropNext")
                if body.get("disconnect"):
                    await asyncio.gather(*(ws.close() for ws in list(sockets)))
            return web.json_response({
                "receipts": receipts, "registeredTools": registry.tool_names,
                "operations": [op.snapshot() for op in service.manager.operations.values()],
                "servers": service.list()["servers"],
            })

        async def websocket(request):
            nonlocal drop_next
            value = request.query.get("ticket", "")
            if value not in tickets:
                raise web.HTTPUnauthorized()
            tickets.remove(value)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.add(ws)
            try:
                async for message in ws:
                    if message.type != WSMsgType.TEXT:
                        continue
                    payload = json.loads(message.data)
                    if payload.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                        continue
                    if payload.get("type") != "rpc":
                        continue
                    method = payload.get("method")
                    params = payload.get("params", {})
                    reply = {"type": "rpc", "id": payload.get("id")}
                    try:
                        if method == "system.capabilities":
                            result = {"version": "android-test-fixture", "featureMethods": methods}
                        elif method in methods:
                            # No arbitrary launches, provider auth or owner files in this fixture.
                            if "config" in params or "envValues" in params or "redirectUri" in params:
                                raise feature_rpc.FeatureRpcError("INVALID", "Fixture accepts stored configuration only")
                            if "name" in params and params["name"] != "android_fixture":
                                raise feature_rpc.FeatureRpcError("INVALID", "Unknown fixture connection")
                            result, _ = await feature_rpc.dispatch(method, params)
                        else:
                            raise feature_rpc.FeatureRpcError("UNKNOWN_METHOD", "Unsupported fixture method")
                        receipts.append({"method": method, "requestId": params.get("requestId"), "outcome": "ok"})
                        reply["result"] = result
                    except feature_rpc.FeatureRpcError as error:
                        receipts.append({"method": method, "outcome": error.code})
                        reply["error"] = {"code": error.code, "message": "Fixture RPC rejected", "retryable": False}
                    if method == drop_next:
                        drop_next = None
                        await ws.close()
                        break
                    await ws.send_json(reply)
            finally:
                sockets.discard(ws)
            return ws

        app = web.Application()
        app.router.add_post("/api/auth/ws-ticket", ticket)
        app.router.add_get("/ws", websocket)
        app.router.add_route("*", "/test/state", control)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", port).start()
            # A disposable fixture credential, never a real gateway/account token.
            print(json.dumps({"port": port, "fixtureToken": token, "isolatedHome": directory}), flush=True)
            await asyncio.Event().wait()
        finally:
            await asyncio.gather(*(ws.close() for ws in list(sockets)))
            await runner.cleanup()
            await service.close()
            await asyncio.to_thread(shutdown_mcp_servers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18997)
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    asyncio.run(stdio_server() if args.stdio else serve(args.port))
