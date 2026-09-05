"""Error/success separation through real SDK stdio, HTTP and SSE peers."""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import sys
import threading

import pytest
from mcp import types
from mcp.server.mcpserver import MCPServer

from flowly.agent.tools.registry import ToolRegistry
from flowly.mcp import client

OPAQUE_CREDENTIAL = "opaque-test-credential"


def make_server():
    server = MCPServer("diagnostic-fixture")

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def inspect(kind: str) -> types.CallToolResult:
        """Return deliberately sensitive errors, or legitimate success data."""
        if kind == "error":
            return types.CallToolResult(
                isError=True,
                content=[
                    types.TextContent(text='Provider failed: {"refresh_token":"hidden-value"} ' + OPAQUE_CREDENTIAL),
                    types.ImageContent(data="cHJpdmF0ZS1iaW5hcnk=", mimeType="image/png"),
                ],
                structuredContent={"api_key": "hidden-structured"},
                _meta={"token": "hidden-meta"},
            )
        if kind == "oversize":
            return types.CallToolResult(isError=True, content=[types.TextContent(text="private-fragment" * 10_000)])
        return types.CallToolResult(
            content=[types.TextContent(text='{"api_key":"legitimate-success-data"}')],
            structuredContent={"token": "legitimate-success-data"},
            _meta={"vendor": "retained"},
        )

    return server


@pytest.fixture(params=["stdio-auto", "stdio-legacy", "stdio-stateless", "http-auto", "http-legacy", "sse-legacy"])
async def connected_peer(tmp_path, monkeypatch, request):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    transport, protocol = request.param.split("-")
    config = {"protocol": protocol, "connect_timeout": 5, "timeout": 3, "osv_check": False}
    server = thread = listener = None
    try:
        if transport == "stdio":
            config.update({
                "command": sys.executable,
                "args": ["-c", "from tests.mcp.test_diagnostic_transport import make_server; make_server().run()"],
                "env": {"VENDOR_AUTH": OPAQUE_CREDENTIAL},
            })
        else:
            import uvicorn

            mcp = make_server()
            app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app(stateless_http=False, json_response=True)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            async with asyncio.timeout(5):
                while not server.started:
                    assert thread.is_alive()
                    await asyncio.sleep(0.01)
            port = listener.getsockname()[1]
            config.update({
                "url": f"http://127.0.0.1:{port}/{'sse' if transport == 'sse' else 'mcp'}",
                "transport": "sse" if transport == "sse" else "http",
                "headers": {"X-Test-Credential": OPAQUE_CREDENTIAL},
            })
        registry = ToolRegistry()
        assert await asyncio.to_thread(
            client.discover_mcp_tools, servers={"diagnostic": config}, tool_registry=registry,
        ) == ["mcp_diagnostic_inspect"]
        yield registry.get("mcp_diagnostic_inspect"), tmp_path / "home"
    finally:
        await asyncio.to_thread(client.shutdown_mcp_servers)
        client._reset_server_error("diagnostic")
        if server is not None:
            server.should_exit = True
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
            assert not thread.is_alive()
        if listener is not None:
            listener.close()


@pytest.mark.parametrize("native", [False, True])
async def test_real_error_results_are_bounded_redacted_and_do_not_cache_attachments(connected_peer, native):
    original, home = connected_peer
    tool = copy.copy(original)
    tool._bridge_native_result = native
    for kind in ("error", "oversize"):
        result = await tool.execute(kind=kind)
        payload = json.loads(result)
        assert payload["isError"] is True
        assert len(result) < 8192
        for secret in (OPAQUE_CREDENTIAL, "hidden-value", "hidden-structured", "hidden-meta", "cHJpdmF0ZS1iaW5hcnk=", "private-fragment"):
            assert secret not in result
    assert not list(home.glob("media/mcp/*"))
    # No overbroad scrubbing of successful application data or vendor metadata.
    success = json.loads(await tool.execute(kind="success"))
    assert "legitimate-success-data" in json.dumps(success)
    assert success["_meta"]["vendor"] == "retained"
    if native:
        assert success["content"][0]["type"] == "text"
        assert not success.get("isError")
    else:
        assert success["structuredContent"] == {"token": "legitimate-success-data"}
