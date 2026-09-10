"""Isolated Desktop acceptance peer. No real account, model or user state.

Used by the opt-in Desktop native OAuth round-trip test. Stdout carries only
prefixed test RPC records; everything lives under a fresh temporary directory.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from mcp import types
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route


def output(value):
    print("FLOWLY_TEST_RPC " + json.dumps(value), flush=True)


async def main():
    with tempfile.TemporaryDirectory(prefix="flowly-desktop-oauth-") as temporary:
        os.environ["FLOWLY_HOME"] = temporary
        from flowly.agent.tools.registry import ToolRegistry
        from flowly.channels import feature_rpc
        from flowly.mcp.client import shutdown_mcp_servers

        registry = ToolRegistry()
        peer = MCPServer("desktop-oauth-acceptance")

        @peer.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
        def echo(message: str) -> str:
            return message

        @peer.tool(annotations=types.ToolAnnotations(readOnlyHint=False))
        def mutate() -> str:
            raise AssertionError("Unselected tool must never execute")

        mcp_app = peer.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        pending_codes = {}
        authorizations = 0
        exchanges = 0
        chat_task = None
        lease_task = None
        lease_release = asyncio.Event()

        async def hold_config_lease():
            from flowly.config.transaction import config_write_lock
            with config_write_lock(Path(temporary) / "config.json"):
                await lease_release.wait()

        async def protected(_request):
            return JSONResponse({"resource": base + "/mcp", "authorization_servers": [base]})

        async def metadata(_request):
            return JSONResponse({
                "issuer": base, "authorization_endpoint": base + "/authorize",
                "token_endpoint": base + "/token", "registration_endpoint": base + "/register",
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
            })

        async def register(request: Request):
            return JSONResponse({"client_id": "desktop-public-client", **await request.json()}, status_code=201)

        async def authorize(request: Request):
            nonlocal authorizations
            authorizations += 1
            query = dict(request.query_params)
            assert query["code_challenge_method"] == "S256"
            code = secrets.token_urlsafe(32)
            pending_codes[code] = query
            return RedirectResponse(query["redirect_uri"] + "?" + urlencode({"code": code, "state": query["state"], "iss": base}))

        async def token(request: Request):
            nonlocal exchanges
            form = await request.form()
            assert form["grant_type"] == "authorization_code"
            issued = pending_codes.pop(form["code"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            assert challenge == issued["code_challenge"]
            assert form["redirect_uri"] == issued["redirect_uri"]
            exchanges += 1
            return JSONResponse({"access_token": "test-access-grant", "token_type": "Bearer", "expires_in": 3600})

        async def gated_mcp(scope, receive, send):
            headers = dict(scope.get("headers", []))
            if headers.get(b"authorization") != b"Bearer test-access-grant":
                response = JSONResponse({"error": "authorization_required"}, status_code=401, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"',
                })
                await response(scope, receive, send)
            else:
                await mcp_app(scope, receive, send)

        @asynccontextmanager
        async def lifespan(_app):
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource", protected),
            Route("/.well-known/oauth-protected-resource/mcp", protected),
            Route("/.well-known/oauth-authorization-server", metadata),
            Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize), Route("/token", token, methods=["POST"]),
            Mount("/", app=gated_mcp),
        ], lifespan=lifespan)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        feature_rpc.set_mcp_connection_runtime(lambda: registry)
        config_path = Path(temporary) / "config.json"
        config_path.write_text(json.dumps({"channels": {"email": {"enabled": True, "imapHost": "unchanged.test"}}}))
        gateway_runner = None
        gateway_port = None
        if os.environ.get("FLOWLY_MCP_TEST_SSH") == "1":
            from aiohttp import web
            from flowly.gateway.server import GatewayServer

            gateway = GatewayServer(auth_token="desktop-ssh-test-token", advertise_control=False)
            gateway_runner = web.AppRunner(gateway._create_app())
            await gateway_runner.setup()
            site = web.TCPSite(gateway_runner, "127.0.0.1", 0)
            await site.start()
            gateway_port = site._server.sockets[0].getsockname()[1]
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
            output({"ready": True, "url": base + "/mcp", "gatewayPort": gateway_port})
            while line := await asyncio.to_thread(sys.stdin.readline):
                request = json.loads(line)
                if request.get("method") == "test.close":
                    break
                try:
                    if request["method"] == "test.config.path":
                        result = str(config_path)
                    elif request["method"] == "test.config.publish":
                        store = feature_rpc._mcp_connection_service.manager.store
                        entry, revision = store.entry("demo")
                        store.publish("demo", entry, revision, lambda: None)
                        result = {"ok": True}
                    elif request["method"] == "test.config.hold":
                        lease_release.clear()
                        lease_task = asyncio.create_task(hold_config_lease())
                        await asyncio.sleep(0)
                        result = {"held": not lease_task.done()}
                    elif request["method"] == "test.config.release":
                        lease_release.set()
                        await lease_task
                        result = {"ok": True}
                    elif request["method"] == "test.chat.propose":
                        from flowly.agent.tool_context import tool_execution_scope
                        from flowly.agent.tools.mcp_connection import MCPConnectionRequestTool
                        assert chat_task is None or chat_task.done()
                        with tool_execution_scope("web:acceptance"):
                            chat_task = asyncio.create_task(MCPConnectionRequestTool().execute(
                                action="request", name="demo", reason="Connect the project notes service",
                                **request.get("params", {}),
                            ))
                        await asyncio.sleep(0)
                        result = feature_rpc._mcp_connection_service.chat.pending("web:acceptance")
                    elif request["method"] == "test.chat.result":
                        result = json.loads(await asyncio.wait_for(chat_task, 2))
                    elif request["method"] == "test.state":
                        config = json.loads(config_path.read_text())
                        result = {"authorizations": authorizations, "exchanges": exchanges,
                            "mailUnchanged": config["channels"] == {"email": {"enabled": True, "imapHost": "unchanged.test"}},
                            "tools": registry.tool_names,
                        }
                    elif request["method"] == "test.echo":
                        result = await registry.execute("mcp_demo_echo", {"message": "round-trip-ok"})
                    elif request["method"].startswith("mcp."):
                        result, restart = await feature_rpc.dispatch(request["method"], request.get("params", {}))
                        assert restart is False
                    else:
                        raise ValueError("Unknown acceptance request")
                    output({"id": request["id"], "result": result})
                except Exception as exc:
                    output({"id": request["id"], "error": str(exc)})
        finally:
            if gateway_runner:
                await gateway_runner.cleanup()
            lease_release.set()
            if lease_task:
                await lease_task
            await feature_rpc.close_mcp_connection_runtime()
            await asyncio.to_thread(shutdown_mcp_servers)
            server.should_exit = True
            await serving
            listener.close()


if __name__ == "__main__":
    asyncio.run(main())
