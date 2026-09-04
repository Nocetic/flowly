"""End-to-end Streamable HTTP coverage against the current MCP SDK."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest


class _Registry:
    def __init__(self):
        self.tools = {}

    def has(self, name):
        return name in self.tools

    def register(self, tool):
        self.tools[tool.name] = tool

    def unregister(self, name):
        self.tools.pop(name, None)


@pytest.fixture(autouse=True)
def reset_mcp():
    yield
    from flowly.mcp import shutdown_mcp_servers

    shutdown_mcp_servers()


@pytest.fixture
def streamable_server():
    import uvicorn
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("flowly-http-test")

    @mcp.tool(structured_output=True)
    def echo(message: str) -> dict[str, str]:
        """Echo a message over Streamable HTTP."""
        return {"echo": message}

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("local Streamable HTTP server did not start")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "local Streamable HTTP server did not stop"


def test_streamable_http_auto_discovery_and_tool_call(streamable_server):
    from flowly.mcp import discover_mcp_tools, get_mcp_server_health

    registry = _Registry()
    names = discover_mcp_tools(
        servers={
            "remote": {
                "url": streamable_server,
                "transport": "http",
                "protocol": "auto",
                "timeout": 5,
                "connect_timeout": 5,
            },
        },
        tool_registry=registry,
    )

    assert names == ["mcp_remote_echo"]
    result = json.loads(asyncio.run(registry.tools["mcp_remote_echo"].execute(message="hello")))
    assert result["structuredContent"] == {"echo": "hello"}

    health = get_mcp_server_health()["remote"]
    assert health["connected"] is True
    assert health["protocolEra"] == "modern"
    assert health["protocolVersion"] == "2026-07-28"
