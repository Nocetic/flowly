"""Real-client interoperability for Flowly's Streamable HTTP server."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
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
def reset_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.mcp.server import readplane

    readplane.get_session_reader.cache_clear()
    yield
    from flowly.mcp import shutdown_mcp_servers

    shutdown_mcp_servers()
    readplane.get_session_reader.cache_clear()


@pytest.fixture
def authenticated_flowly_server():
    from flowly.mcp.server.serve import create_server
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    token = "test-secret-" + "x" * 32
    mcp = create_server(auth_token=token, resource_url=url)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )
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
        pytest.fail("Flowly Streamable HTTP server did not start")

    try:
        yield url, token
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "Flowly Streamable HTTP server did not stop"


def test_http_requires_bearer_auth(authenticated_flowly_server):
    url, _token = authenticated_flowly_server
    response = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401


def test_current_client_discovers_and_calls_flowly_over_http(
    authenticated_flowly_server,
) -> None:
    from flowly.mcp import discover_mcp_tools, get_mcp_server_health

    url, token = authenticated_flowly_server
    registry = _Registry()
    names = discover_mcp_tools(
        servers={
            "bridge": {
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
                "transport": "http",
                "protocol": "auto",
                "timeout": 5,
                "connect_timeout": 5,
            },
        },
        tool_registry=registry,
    )

    assert len(names) == 5
    assert "mcp_bridge_channels_list" in names
    result = json.loads(asyncio.run(registry.tools["mcp_bridge_channels_list"].execute()))
    assert result["structuredContent"]["count"] >= 1
    assert get_mcp_server_health()["bridge"]["protocolEra"] == "modern"
