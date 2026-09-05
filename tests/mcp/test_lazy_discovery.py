"""Public client discovery, process restart, lazy wake and initial-start races."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.agent.tools.registry import ToolRegistry
from flowly.mcp import client

SERVER = r'''
import asyncio, json, os, sys
from pathlib import Path
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

state_file, log_file = map(Path, sys.argv[1:])
def state():
    return json.loads(state_file.read_text())
def log(event):
    with log_file.open("a") as out:
        out.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")
log("spawn")
async def listing(ctx, params):
    value = state()
    await asyncio.sleep(value.get("discovery_delay", 0))
    return types.ListToolsResult(tools=[types.Tool(
        name="read", description="Version " + str(value.get("version", 1)),
        inputSchema={"type": "object"},
        annotations=types.ToolAnnotations(readOnlyHint=value.get("readonly", True)),
    )])
async def call(ctx, params):
    log("call")
    return types.CallToolResult(content=[types.TextContent(text="read")])
async def main():
    server = Server("lazy", on_list_tools=listing, on_call_tool=call)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
asyncio.run(main())
'''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state, log = tmp_path / "state.json", tmp_path / "events.jsonl"
    state.write_text("{}")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    config = {"command": sys.executable, "args": ["-c", SERVER, str(state), str(log)],
              "timeout": 3, "connect_timeout": 5, "osv_check": False,
              "lifecycle": {"lazy_start": True, "manifest_ttl": 60}}
    yield config, home, state, log
    client.shutdown_mcp_servers()
    client._reset_server_error("lazy")


def discover(config, **kwargs):
    registry = ToolRegistry()
    assert client.discover_mcp_tools(servers={"lazy": config}, tool_registry=registry, **kwargs) == [
        "mcp_lazy_read",
    ]
    return registry


def events(log, kind):
    if not log.exists():
        return []
    return [event for line in log.read_text().splitlines()
            if (event := json.loads(line))["event"] == kind]


@pytest.mark.parametrize("mode", ["auto", "legacy", "stateless"])
def test_public_warm_boot_uses_manifest_then_revalidates_on_first_call(setup, mode):
    config, home, _, log = setup
    config["protocol"] = mode
    discover(config)
    assert len(events(log, "spawn")) == 1
    assert list((home / "cache" / "mcp-manifests").glob("*.json"))
    client.shutdown_mcp_servers()
    registry = discover(config)
    assert client.get_mcp_server_health()["lazy"]["catalogSource"] == "manifest"
    assert client.get_mcp_server_health()["lazy"]["connected"] is False
    assert len(events(log, "spawn")) == 1
    assert json.loads(asyncio.run(registry.execute("mcp_lazy_read", {})))["result"] == "read"
    assert client.get_mcp_server_health()["lazy"]["catalogSource"] == "live"
    assert len(events(log, "spawn")) == 2


@pytest.mark.parametrize("change", [{"version": 2}, {"readonly": False}])
def test_cached_contract_cannot_execute_changed_live_tool(setup, change):
    config, _, state, log = setup
    config["trust"] = "untrusted"
    discover(config)
    client.shutdown_mcp_servers()
    state.write_text(json.dumps(change))
    registry = discover(config)
    assert registry.get("mcp_lazy_read").description == "Version 1"
    result = json.loads(asyncio.run(registry.execute("mcp_lazy_read", {})))
    assert "changed" in result["error"]
    assert not events(log, "call")
    if "version" in change:
        assert registry.get("mcp_lazy_read").description == "Version 2"
        assert "result" in json.loads(asyncio.run(registry.execute("mcp_lazy_read", {})))
    else:
        assert registry.get("mcp_lazy_read").annotations["readOnlyHint"] is False
        assert "not approved" in json.loads(asyncio.run(registry.execute("mcp_lazy_read", {})))["error"]
        assert not events(log, "call")


@pytest.mark.parametrize("change", [
    {"trust": "untrusted"}, {"env": {"CACHE_BINDING": "changed"}},
    {"headers": {"X-Context": "changed"}}, {"protocol": "legacy"},
])
def test_config_change_invalidates_cache_and_live_reuse(setup, change):
    config, _, _, log = setup
    discover(config)
    other = ToolRegistry()
    assert client.discover_mcp_tools(servers={"lazy": config | change}, tool_registry=other) == []
    assert other.get("mcp_lazy_read") is None
    assert len(events(log, "spawn")) == 1
    client.shutdown_mcp_servers()
    discover(config | change)
    assert len(events(log, "spawn")) == 2
    assert client.get_mcp_server_health()["lazy"]["catalogSource"] == "live"


@pytest.mark.parametrize("kind", ["corrupt", "expired", "disabled", "interactive", "probe"])
def test_cache_miss_disabled_lazy_and_explicit_probes_still_connect(setup, kind):
    config, home, _, log = setup
    discover(config)
    client.shutdown_mcp_servers()
    manifest = next((home / "cache" / "mcp-manifests").glob("*.json"))
    if kind == "corrupt":
        manifest.write_text("not JSON")
    elif kind == "expired":
        document = json.loads(manifest.read_text())
        document["observedAt"] = 0
        manifest.write_text(json.dumps(document))
    elif kind == "disabled":
        config["lifecycle"]["lazy_start"] = False
    if kind == "probe":
        from flowly.mcp.probe import probe_tool_names

        ok, names, error = probe_tool_names("lazy", config)
        assert ok and names == ["read"], error
    else:
        discover(config, interactive=kind == "interactive")
    assert len(events(log, "spawn")) == 2


def test_simultaneous_first_discoveries_share_one_process_and_all_registries(setup):
    config, _, state, log = setup
    state.write_text('{"discovery_delay": 0.15}')
    barrier = threading.Barrier(8)

    def run():
        barrier.wait(timeout=5)
        return discover(config)

    with ThreadPoolExecutor(max_workers=8) as pool:
        registries = list(pool.map(lambda _: run(), range(8)))
    assert len(events(log, "spawn")) == 1
    task = client._servers["lazy"]
    assert len(task._registry_bindings) == 8
    assert not client._starting
    assert all(registry.get("mcp_lazy_read")._server_task is task for registry in registries)


def test_explicit_discovery_of_an_existing_dormant_runtime_does_not_return_cached_readiness(setup):
    config, _, _, log = setup
    discover(config)
    client.shutdown_mcp_servers()
    discover(config)
    assert client.get_mcp_server_health()["lazy"]["catalogSource"] == "manifest"
    discover(config, interactive=True)
    assert client.get_mcp_server_health()["lazy"]["catalogSource"] == "live"
    assert len(events(log, "spawn")) == 2


async def test_cancelled_initial_waiter_does_not_cancel_a_peer(setup):
    from flowly.mcp.manifest import configuration_fingerprint

    config, home, state, log = setup
    state.write_text('{"discovery_delay": 0.2}')
    identity = configuration_fingerprint("lazy", config, home)
    loop = client._ensure_loop()
    first, second = ToolRegistry(), ToolRegistry()
    a = asyncio.run_coroutine_threadsafe(client._shared_server("lazy", config, identity, first, False), loop)
    b = asyncio.run_coroutine_threadsafe(client._shared_server("lazy", config, identity, second, False), loop)
    async with asyncio.timeout(5):
        while not events(log, "spawn"):
            await asyncio.sleep(0.01)
    a.cancel()
    assert await asyncio.wrap_future(b) == ["mcp_lazy_read"]
    assert len(events(log, "spawn")) == 1
    assert first.get("mcp_lazy_read") is None
    assert json.loads(await second.execute("mcp_lazy_read", {}))["result"] == "read"


async def test_last_cancelled_initial_waiter_joins_cleanup_before_retry(setup):
    from flowly.mcp.manifest import configuration_fingerprint

    config, home, state, log = setup
    state.write_text('{"discovery_delay": 10}')
    loop = client._ensure_loop()
    first = asyncio.run_coroutine_threadsafe(client._shared_server(
        "lazy", config, configuration_fingerprint("lazy", config, home), ToolRegistry(), False,
    ), loop)
    async with asyncio.timeout(5):
        while not events(log, "spawn"):
            await asyncio.sleep(0.01)
    first.cancel()
    async with asyncio.timeout(5):
        while "lazy" in client._servers or "lazy" in client._starting:
            await asyncio.sleep(0.01)
    pid = events(log, "spawn")[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    state.write_text("{}")
    registry = await asyncio.to_thread(discover, config)
    assert "result" in json.loads(await registry.execute("mcp_lazy_read", {}))
    assert len(events(log, "spawn")) == 2


def test_two_independent_client_processes_share_only_manifest_not_runtime(setup, tmp_path):
    config, _, _, log = setup
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    script = '''
import json, sys
from pathlib import Path
from flowly.agent.tools.registry import ToolRegistry
from flowly.mcp import client
registry = ToolRegistry()
names = client.discover_mcp_tools(servers={"lazy": json.loads(Path(sys.argv[1]).read_text())}, tool_registry=registry)
print(json.dumps({"names": names, "source": client.get_mcp_server_health()["lazy"]["catalogSource"]}))
client.shutdown_mcp_servers()
'''
    sources = []
    for _ in range(2):
        completed = subprocess.run([sys.executable, "-c", script, str(config_file)],
                                   capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["names"] == ["mcp_lazy_read"]
        sources.append(result["source"])
    assert sources == ["live", "manifest"]
    assert len(events(log, "spawn")) == 1


async def test_shutdown_tracks_startup_even_before_it_has_registered_tools(setup):
    config, _, state, log = setup
    state.write_text('{"discovery_delay": 10}')
    registry = ToolRegistry()
    discovery = asyncio.create_task(asyncio.to_thread(client.discover_mcp_tools,
        servers={"lazy": config}, tool_registry=registry))
    async with asyncio.timeout(5):
        while not events(log, "spawn"):
            await asyncio.sleep(0.01)
    await asyncio.to_thread(client.shutdown_mcp_servers)
    assert await discovery == []
    assert not client._starting and not client._servers
    assert registry.get("mcp_lazy_read") is None
    with pytest.raises(ProcessLookupError):
        os.kill(events(log, "spawn")[0]["pid"], 0)


async def test_shutdown_observation_timeout_keeps_cleanup_owned_and_blocks_new_startup(setup, monkeypatch):
    config, _, _, log = setup
    await asyncio.to_thread(discover, config)
    server = client._servers["lazy"]
    loop = client.get_mcp_loop()
    original = server.shutdown
    release = asyncio.Event()
    entered = threading.Event()

    async def slow_shutdown():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(server, "shutdown", slow_shutdown)
    try:
        await asyncio.to_thread(client.shutdown_mcp_servers, 0)
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert client._shutting_down
        assert client.get_mcp_loop() is loop and loop.is_running()
        assert client.discover_mcp_tools(servers={"new": config}, tool_registry=ToolRegistry()) == []
        assert len(events(log, "spawn")) == 1
    finally:
        loop.call_soon_threadsafe(release.set)
        async with asyncio.timeout(5):
            while loop.is_running() or client._shutting_down:
                await asyncio.sleep(0.005)
    assert not client._servers and not client._starting
    with pytest.raises(ProcessLookupError):
        os.kill(events(log, "spawn")[0]["pid"], 0)


def test_cached_runtime_will_not_wake_under_a_different_profile(setup, monkeypatch):
    config, home, _, log = setup
    discover(config)
    client.shutdown_mcp_servers()
    registry = discover(config)
    monkeypatch.setenv("FLOWLY_HOME", str(home / "different-profile"))
    result = json.loads(asyncio.run(registry.execute("mcp_lazy_read", {})))
    assert "error" in result
    assert len(events(log, "spawn")) == 1
    assert not events(log, "call")


def test_manifest_warm_boot_concurrent_consumers_and_idle_recycling_share_one_lifecycle(setup):
    config, _, _, log = setup
    config["lifecycle"]["idle_timeout"] = 0.03
    discover(config)
    client.shutdown_mcp_servers()
    with ThreadPoolExecutor(max_workers=8) as pool:
        registries = list(pool.map(lambda _: discover(config), range(8)))
    assert len(events(log, "spawn")) == 1
    assert client.get_mcp_server_health()["lazy"]["catalogSource"] == "manifest"

    async def run():
        async def idle():
            async with asyncio.timeout(5):
                while client.get_mcp_server_health()["lazy"]["state"] != "idle":
                    await asyncio.sleep(0.005)

        results = await asyncio.gather(*[registry.execute("mcp_lazy_read", {}) for registry in registries])
        assert all(json.loads(result)["result"] == "read" for result in results)
        await idle()
        assert len(events(log, "spawn")) == 2
        assert json.loads(await registries[0].execute("mcp_lazy_read", {}))["result"] == "read"
        await idle()
        assert len(events(log, "spawn")) == 3
        assert client.get_mcp_server_health()["lazy"]["activeRequests"] == 0

    asyncio.run(run())
