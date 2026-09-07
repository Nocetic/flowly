"""Reconfigure one real stdio server while other connections and calls live."""
import asyncio
import copy
import json
import sys

import pytest

from flowly.mcp import client

SERVER = '''
import asyncio
from mcp.server.mcpserver import MCPServer
mcp = MCPServer("reload-test")
@mcp.tool()
def echo(value: str) -> str:
    return value
@mcp.tool()
async def slow(value: str) -> str:
    await asyncio.sleep(0.25)
    return value
mcp.run()
'''


class Registry:
    def __init__(self):
        self.tools = {}
    def has(self, name):
        return name in self.tools
    def register(self, tool):
        self.tools[tool.name] = tool


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    config = {"command": sys.executable, "args": [str(script)], "connect_timeout": 10, "timeout": 5}
    registry = Registry()
    yield config, registry
    client.shutdown_mcp_servers()


@pytest.mark.asyncio
async def test_reload_preserves_peer_and_rejects_late_discovery(runtime):
    config, registry = runtime
    await asyncio.to_thread(client.discover_mcp_tools, servers={"one": config, "peer": config}, tool_registry=registry)
    peer = client._servers["peer"]
    peer_session = peer.session
    old_tool = registry.tools["mcp_one_slow"]
    restricted = {**config, "tools": {"mode": "selected", "include": ["echo"]}}
    result = await client.reload_mcp_server("one", restricted, registry)
    assert result["ok"] is True
    assert "mcp_one_echo" in registry.tools
    assert "mcp_one_slow" not in registry.tools
    assert client._servers["peer"] is peer
    assert peer.session is peer_session
    # A still-running initial discovery worker must not undo owner permissions.
    assert await asyncio.to_thread(client.discover_mcp_tools, servers={"one": config}, tool_registry=registry) == []
    assert "mcp_one_slow" not in registry.tools
    assert "error" in json.loads(await old_tool.execute(value="must not run"))
    assert "peer works" in await registry.tools["mcp_peer_echo"].execute(value="peer works")


@pytest.mark.asyncio
async def test_reload_drains_sent_call_before_closing_transport(runtime):
    config, registry = runtime
    await asyncio.to_thread(client.discover_mcp_tools, servers={"one": config}, tool_registry=registry)
    old = client._servers["one"]
    running = asyncio.create_task(registry.tools["mcp_one_slow"].execute(value="finished safely"))
    async with asyncio.timeout(3):
        while not old._inflight_tool_calls:
            await asyncio.sleep(0.005)
    replacing = asyncio.create_task(client.reload_mcp_server("one", None, registry))
    async with asyncio.timeout(3):
        while not old._retiring:
            await asyncio.sleep(0.005)
    assert not replacing.done()
    assert "mcp_one_slow" not in registry.tools
    assert "finished safely" in await running
    assert (await replacing)["state"] == "disabled"
    assert "one" not in client._servers


@pytest.mark.asyncio
async def test_rebinds_other_consumers_without_removing_foreign_tools(runtime):
    config, registry = runtime
    secondary = Registry()
    await asyncio.to_thread(client.discover_mcp_tools, servers={"one": config}, tool_registry=registry)
    await asyncio.to_thread(client.discover_mcp_tools, servers={"one": config}, tool_registry=secondary)
    foreign = object()
    registry.tools["foreign"] = foreign
    changed = copy.deepcopy(config)
    changed["tools"] = {"mode": "selected", "include": ["echo"]}
    await client.reload_mcp_server("one", changed, registry)
    assert "mcp_one_echo" in secondary.tools
    assert "mcp_one_slow" not in secondary.tools
    assert registry.tools["foreign"] is foreign


@pytest.mark.asyncio
async def test_disabled_tombstone_does_not_survive_runtime_shutdown(runtime):
    config, registry = runtime
    await client.reload_mcp_server("one", None, registry)
    client.shutdown_mcp_servers()
    assert client._desired_identities == {}
    names = await asyncio.to_thread(client.discover_mcp_tools, servers={"one": config}, tool_registry=registry)
    assert "mcp_one_echo" in names
