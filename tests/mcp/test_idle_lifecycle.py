"""Demand-driven connection recycling, request leases and stale contracts."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from flowly.mcp import client
from flowly.mcp.lifecycle import MCPConnectionState, MCPRetryPolicy, MCPUnavailableError


async def _state(task, state):
    async with asyncio.timeout(3):
        while task.state is not state:
            await asyncio.sleep(0.005)


@asynccontextmanager
async def _server(monkeypatch, *, idle=0.02, lifetime=0, connect_timeout=0.5):
    task = client.MCPServerTask("recycle-fixture")
    sessions = []
    closed = []

    async def transport():
        async def list_tools():
            return SimpleNamespace(tools=[])

        session = SimpleNamespace(list_tools=list_tools)
        sessions.append(session)
        try:
            await task._serve_connected(session)
        finally:
            closed.append(session)

    monkeypatch.setattr(task, "_run_transport", transport)
    await task.start({
        "command": "unused",
        "connect_timeout": connect_timeout,
        "lifecycle": {
            "idle_timeout": idle,
            "max_lifetime": lifetime,
            "keepalive_interval": 0.005,
            "reconnect_base_delay": 0.01,
            "reconnect_max_delay": 0.01,
            "reconnect_jitter": 0,
            "close_timeout": 0.04,
        },
    })
    try:
        yield task, sessions, closed
    finally:
        await task.shutdown()


@pytest.mark.parametrize("field", ["idle_timeout", "max_lifetime", "close_timeout"])
@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan"), 604_801])
def test_policy_rejects_unbounded_recycle_timers(field, bad):
    from flowly.config.schema import MCPServerLifecycleConfig

    with pytest.raises(ValueError):
        MCPRetryPolicy(**{field: bad})
    with pytest.raises(ValueError):
        MCPServerLifecycleConfig(**{field: bad})


@pytest.mark.parametrize("value", [0, 61])
def test_teardown_deadline_is_always_positive_and_bounded(value):
    from flowly.config.schema import MCPServerLifecycleConfig

    for cls in (MCPRetryPolicy, MCPServerLifecycleConfig):
        with pytest.raises(ValueError):
            cls(close_timeout=value)


async def test_idle_closes_after_keepalives_and_does_not_restart(monkeypatch):
    async with _server(monkeypatch) as (task, sessions, closed):
        await _state(task, MCPConnectionState.IDLE)
        assert closed == sessions
        assert task.session is None
        await asyncio.sleep(0.06)
        assert len(sessions) == 1
        assert task.health_snapshot()["recycleCount"] == 1


async def test_concurrent_cold_calls_share_one_transport_and_cancel_independently(monkeypatch):
    async with _server(monkeypatch) as (task, sessions, closed):
        await _state(task, MCPConnectionState.IDLE)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def request():
            async with task.connection_lease() as session:
                entered.set()
                await release.wait()
                return session

        requests = [asyncio.create_task(request()) for _ in range(20)]
        try:
            await entered.wait()
            requests[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await requests[0]
            await asyncio.sleep(0.06)
            assert len(sessions) == 2
            assert len(closed) == 1
            assert task.health_snapshot()["activeRequests"] == 19
            release.set()
            assert all(session is sessions[-1] for session in await asyncio.gather(*requests[1:]))
            await _state(task, MCPConnectionState.IDLE)
            assert len(closed) == 2
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)


async def test_lifetime_drains_current_calls_but_new_requests_use_next_session(monkeypatch):
    async with _server(monkeypatch, idle=0, lifetime=0.04) as (task, sessions, closed):
        async with task.connection_lease() as old:
            await _state(task, MCPConnectionState.DRAINING)
            admitted = asyncio.Event()

            async def next_request():
                async with task.connection_lease() as new:
                    admitted.set()
                    return new

            waiter = asyncio.create_task(next_request())
            await asyncio.sleep(0.03)
            assert not admitted.is_set()
            assert not closed
        new = await asyncio.wait_for(waiter, 1)
        assert new is not old
        assert closed == [old]
        assert len(sessions) == 2
        await _state(task, MCPConnectionState.IDLE)
        await asyncio.sleep(0.06)
        assert len(sessions) == 2  # Lifetime expiry does not start an unused process.


async def test_cancelled_drain_waiter_does_not_restart_an_unused_server(monkeypatch):
    async with _server(monkeypatch, idle=0, lifetime=0.02) as (task, sessions, _):
        async with task.connection_lease():
            await _state(task, MCPConnectionState.DRAINING)

            async def request():
                async with task.connection_lease():
                    pytest.fail("Request was admitted while the old session was draining")

            waiter = asyncio.create_task(request())
            await asyncio.sleep(0.01)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        await _state(task, MCPConnectionState.IDLE)
        await asyncio.sleep(0.06)
        assert len(sessions) == 1
        assert task.health_snapshot()["waitingRequests"] == 0


async def test_drain_wait_deadline_does_not_kill_the_active_call(monkeypatch):
    async with _server(monkeypatch, lifetime=0.02, connect_timeout=0.06) as (
        task, sessions, closed,
    ):
        async with task.connection_lease():
            await _state(task, MCPConnectionState.DRAINING)
            with pytest.raises(MCPUnavailableError, match="connection"):
                async with task.connection_lease():
                    pytest.fail("Should time out before admission")
            assert not closed
            assert task.session is sessions[0]
        await _state(task, MCPConnectionState.IDLE)


async def test_zero_timers_preserve_persistent_connection(monkeypatch):
    async with _server(monkeypatch, idle=0, lifetime=0) as (task, sessions, closed):
        await asyncio.sleep(0.06)
        assert task.state is MCPConnectionState.CONNECTED
        assert len(sessions) == 1
        assert not closed


def test_recycling_is_opt_in_for_servers_that_keep_state_in_memory():
    from flowly.config.schema import MCPServerLifecycleConfig

    for settings in (MCPRetryPolicy(), MCPServerLifecycleConfig()):
        assert settings.idle_timeout == 0
        assert settings.max_lifetime == 0


async def test_shutdown_of_idle_server_is_terminal(monkeypatch):
    async with _server(monkeypatch) as (task, sessions, _):
        await _state(task, MCPConnectionState.IDLE)
        await asyncio.wait_for(task.shutdown(), 0.2)
        with pytest.raises(MCPUnavailableError, match="stopped"):
            async with task.connection_lease():
                pytest.fail("Stopped server accepted a request")
        assert len(sessions) == 1


async def test_second_start_does_not_replace_the_supervisor(monkeypatch):
    async with _server(monkeypatch) as (task, sessions, _):
        supervisor = task._task
        with pytest.raises(RuntimeError, match="already started"):
            await task.start({"command": "different"})
        assert task._task is supervisor
        assert task._config["command"] == "unused"
        assert len(sessions) == 1


async def test_reconnect_handshake_timeout_closes_attempt_before_retry(monkeypatch):
    async with _server(monkeypatch, connect_timeout=0.06) as (task, _, _):
        await _state(task, MCPConnectionState.IDLE)
        attempts = 0
        active = 0
        peak = 0

        async def hung_transport():
            nonlocal attempts, active, peak
            attempts += 1
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        monkeypatch.setattr(task, "_run_transport", hung_transport)
        with pytest.raises(MCPUnavailableError):
            async with task.connection_lease():
                pytest.fail("Hung handshake admitted a request")
        async with asyncio.timeout(1):
            while attempts < 2:
                await asyncio.sleep(0.005)
        await task.shutdown()
        assert peak == 1
        assert active == 0


async def test_hung_recycle_teardown_is_cancelled_and_awaited_before_wake(monkeypatch):
    async with _server(monkeypatch) as (task, sessions, _):
        await _state(task, MCPConnectionState.IDLE)
        cleaned = asyncio.Event()

        async def transport():
            async def listing():
                return SimpleNamespace(tools=[])

            session = SimpleNamespace(list_tools=listing)
            sessions.append(session)
            try:
                await task._serve_connected(session)
                await asyncio.Event().wait()  # Simulated unresponsive close handshake.
            finally:
                cleaned.set()

        monkeypatch.setattr(task, "_run_transport", transport)
        async with task.connection_lease():
            pass
        await _state(task, MCPConnectionState.IDLE)
        assert cleaned.is_set()
        assert "recycle teardown timed out" in task.health_snapshot()["lastError"]
        assert len(sessions) == 2
        await asyncio.sleep(0.08)
        assert len(sessions) == 2


async def test_cancelled_call_releases_half_open_probe_without_error(monkeypatch):
    async with _server(monkeypatch) as (task, _, _):
        from flowly.mcp.tool import _run_on_mcp_loop

        monkeypatch.setattr(client, "get_mcp_loop", asyncio.get_running_loop)
        entered = asyncio.Event()

        async def operation(session):
            entered.set()
            await asyncio.Event().wait()

        request = asyncio.create_task(_run_on_mcp_loop(
            server_task=task, tool_name="test", coro_factory=operation,
            timeout=1, on_interrupt=client.MCPCallInterrupted,
        ))
        await entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await _state(task, MCPConnectionState.IDLE)
        assert task.health_snapshot()["activeRequests"] == 0
        assert not client._server_error_counts.get(task.name)


REAL_SERVER = r'''
import asyncio
import json
import os
import sys
from pathlib import Path
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

settings, events = map(Path, sys.argv[1:])
def state():
    return json.loads(settings.read_text())
def event(kind, **data):
    with events.open("a") as out:
        out.write(json.dumps({"event": kind, "pid": os.getpid(), **data}) + "\n")
event("spawn")

async def listing(ctx, params):
    config = state()
    return types.ListToolsResult(tools=[] if not config.get("present", True) else [types.Tool(
        name="work", description="Work version " + str(config.get("version", 1)),
        inputSchema={"type": "object", "properties": {
            "delay": {"type": "number"}, "label": {"type": "string"},
        }}, annotations=types.ToolAnnotations(readOnlyHint=config.get("readonly", True)),
    )])

async def call(ctx, params):
    args = params.arguments or {}
    event("start", label=args.get("label", ""))
    try:
        await asyncio.sleep(args.get("delay", 0))
        event("done", label=args.get("label", ""))
        return types.CallToolResult(content=[types.TextContent(text=json.dumps({
            "pid": os.getpid(), "label": args.get("label", ""),
        }))])
    finally:
        event("release", label=args.get("label", ""))

async def resources(ctx, params):
    return types.ListResourcesResult(resources=[types.Resource(uri="test://slow", name="Slow")])

async def read_resource(ctx, params):
    event("resource:start")
    await asyncio.sleep(0.18)
    event("resource:done")
    return types.ReadResourceResult(contents=[types.TextResourceContents(
        uri=params.uri, text="Read by " + str(os.getpid()),
    )])

async def main():
    kwargs = {}
    if state().get("resources", True):
        kwargs.update(on_list_resources=resources, on_read_resource=read_resource)
    server = Server("recycling", on_list_tools=listing, on_call_tool=call, **kwargs)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

asyncio.run(main())
'''


@pytest.fixture
def real_server(tmp_path, monkeypatch):
    from flowly.agent.tools.registry import ToolRegistry

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    settings, log = tmp_path / "state.json", tmp_path / "events.jsonl"
    settings.write_text("{}")

    def launch(*, mode="auto", idle=0.04, lifetime=0, trust="full"):
        registry = ToolRegistry()
        config = {
            "command": sys.executable, "args": ["-c", REAL_SERVER, str(settings), str(log)],
            "protocol": mode, "osv_check": False, "timeout": 2, "connect_timeout": 5,
            "supports_parallel_tool_calls": True, "max_parallel_tool_calls": 3,
            "trust": trust, "elicitation": {"enabled": False},
            "tools": {"resources": True},
            "lifecycle": {"idle_timeout": idle, "max_lifetime": lifetime},
        }
        names = client.discover_mcp_tools(servers={"recycle": config}, tool_registry=registry)
        assert "mcp_recycle_work" in names
        return registry, client._servers["recycle"], settings, log, config

    yield launch
    client.shutdown_mcp_servers()
    client._reset_server_error("recycle")


def _events(log, kind):
    if not log.exists():
        return []
    return [event for line in log.read_text().splitlines()
            if (event := json.loads(line))["event"] == kind]


async def _event_count(log, kind, count):
    async with asyncio.timeout(3):
        while len(_events(log, kind)) < count:
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("mode", ["auto", "legacy", "stateless"])
async def test_real_stdio_reaps_idle_process_and_wakes_once_for_parallel_calls(real_server, mode):
    registry, task, _, log, _ = real_server(mode=mode)
    await _state(task, MCPConnectionState.IDLE)
    first_pid = _events(log, "spawn")[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)
    results = await asyncio.gather(*[
        registry.execute("mcp_recycle_work", {"delay": 0.02, "label": str(i)})
        for i in range(12)
    ])
    values = [json.loads(json.loads(result)["result"]) for result in results]
    assert len({value["pid"] for value in values}) == 1
    assert values[0]["pid"] != first_pid
    assert {value["label"] for value in values} == {str(i) for i in range(12)}
    assert len(_events(log, "spawn")) == 2
    await _state(task, MCPConnectionState.IDLE)
    with pytest.raises(ProcessLookupError):
        os.kill(values[0]["pid"], 0)


async def test_real_lifetime_drains_request_and_routes_waiter_to_fresh_process(real_server):
    registry, task, _, log, _ = real_server(idle=0, lifetime=0.08)
    first = asyncio.create_task(registry.execute("mcp_recycle_work", {"delay": 0.18}))
    await _event_count(log, "start", 1)
    await _state(task, MCPConnectionState.DRAINING)
    second = asyncio.create_task(registry.execute("mcp_recycle_work", {"label": "next"}))
    first_result, second_result = await asyncio.gather(first, second)
    assert json.loads(json.loads(first_result)["result"])["pid"] != json.loads(
        json.loads(second_result)["result"],
    )["pid"]
    assert len(_events(log, "done")) == 2
    await _state(task, MCPConnectionState.IDLE)


@pytest.mark.parametrize("change", [{"version": 2}, {"readonly": False}, {"present": False}])
async def test_real_cold_schema_or_permission_change_never_executes_old_call(real_server, change):
    registry, task, settings, log, _ = real_server(trust="untrusted")
    old = registry.get("mcp_recycle_work")
    await _state(task, MCPConnectionState.IDLE)
    settings.write_text(json.dumps(change))
    result = json.loads(await registry.execute("mcp_recycle_work", {}))
    assert "changed" in result["error"]
    assert not _events(log, "start")
    assert not client._server_error_counts.get("recycle")
    current = registry.get("mcp_recycle_work")
    assert current is not old
    if "version" in change:
        assert "version 2" in current.description
        assert "result" in json.loads(await registry.execute("mcp_recycle_work", {}))
    elif "readonly" in change:
        assert current.annotations["readOnlyHint"] is False
        # No approving user surface: a retry must not execute the newly writable tool.
        assert "not approved" in json.loads(await registry.execute("mcp_recycle_work", {}))["error"]
        assert not _events(log, "start")
    else:
        assert current is None


async def test_real_unapproved_write_does_not_wake_idle_server(real_server):
    registry, task, settings, log, _ = real_server(trust="untrusted")
    # First obtain a fresh write-capable schema without executing it.
    await _state(task, MCPConnectionState.IDLE)
    settings.write_text('{"readonly": false}')
    assert "changed" in json.loads(await registry.execute("mcp_recycle_work", {}))["error"]
    await _state(task, MCPConnectionState.IDLE)
    launches = len(_events(log, "spawn"))
    assert "not approved" in json.loads(await registry.execute("mcp_recycle_work", {}))["error"]
    await asyncio.sleep(0.06)
    assert task.state is MCPConnectionState.IDLE
    assert len(_events(log, "spawn")) == launches


async def test_real_resource_read_is_pinned_across_idle_and_lifetime_deadlines(real_server):
    registry, task, _, log, _ = real_server(idle=0.03, lifetime=0.06)
    await _state(task, MCPConnectionState.IDLE)
    read = asyncio.create_task(registry.execute("mcp_recycle_read_resource", {"uri": "test://slow"}))
    await _event_count(log, "resource:start", 1)
    await _state(task, MCPConnectionState.DRAINING)
    assert not _events(log, "resource:done")
    result = await read
    assert "Read by" in result, result
    assert "error" not in json.loads(result)
    await _state(task, MCPConnectionState.IDLE)


async def test_real_disappeared_resource_capability_is_rejected_on_wake(real_server):
    registry, task, settings, log, _ = real_server()
    await _state(task, MCPConnectionState.IDLE)
    settings.write_text('{"resources": false}')
    result = json.loads(await registry.execute("mcp_recycle_read_resource", {"uri": "test://slow"}))
    assert "capability changed" in result["error"]
    assert not _events(log, "resource:start")
    assert registry.get("mcp_recycle_read_resource") is None


async def test_real_repeated_discovery_preserves_all_consumer_registries(real_server):
    from flowly.agent.tools.registry import ToolRegistry

    first, task, settings, log, config = real_server()
    second = ToolRegistry()
    for registry in (first, second, first):
        assert "mcp_recycle_work" in client.discover_mcp_tools(
            servers={"recycle": config}, tool_registry=registry,
        )
    await _state(task, MCPConnectionState.IDLE)
    settings.write_text('{"version": 2}')
    assert "changed" in json.loads(await first.execute("mcp_recycle_work", {}))["error"]
    for registry in (first, second):
        assert "version 2" in registry.get("mcp_recycle_work").description
        assert "result" in json.loads(await registry.execute("mcp_recycle_work", {}))
    assert len(_events(log, "spawn")) == 2


def test_metadata_refresh_advances_registry_even_when_model_schema_is_unchanged():
    from flowly.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    task = client.MCPServerTask("metadata")
    task.tools = [_remote(annotations={"readOnlyHint": True})]
    client._register_tools_for_server(server_task=task, server_cfg={}, tool_registry=registry)
    before = registry.get("mcp_metadata_read")
    generation = registry.generation
    task.tools = [_remote(annotations={"readOnlyHint": False})]
    client._reregister_server_tools(task)
    current = registry.get("mcp_metadata_read")
    assert before.to_schema() == current.to_schema()
    assert current.annotations["readOnlyHint"] is False
    assert current is not before
    assert registry.generation > generation


def test_multi_registry_refresh_retains_per_consumer_filters_and_weak_ownership():
    import gc
    import weakref

    from flowly.agent.tools.registry import ToolRegistry

    task = client.MCPServerTask("filtered")
    task.tools = [_remote(), _remote(name="write")]
    first, second = ToolRegistry(), ToolRegistry()
    for registry, include in ((first, ["read"]), (second, ["write"])):
        client._register_tools_for_server(server_task=task, tool_registry=registry, server_cfg={
            "tools": {"include": include},
        })
    task.tools = [_remote(description="Changed"), _remote(name="write", description="Changed")]
    client._reregister_server_tools(task)
    assert first.get("mcp_filtered_read").description == "Changed"
    assert first.get("mcp_filtered_write") is None
    assert second.get("mcp_filtered_write").description == "Changed"
    assert second.get("mcp_filtered_read") is None
    first_ref = weakref.ref(first)
    del first
    gc.collect()
    assert first_ref() is None
    assert len(task._registry_bindings) == 1


async def test_real_cancellation_keeps_another_request_and_its_process_alive(real_server):
    registry, task, _, log, _ = real_server()
    await _state(task, MCPConnectionState.IDLE)
    requests = [asyncio.create_task(registry.execute(
        "mcp_recycle_work", {"delay": delay, "label": label},
    )) for delay, label in ((5, "cancelled"), (0.15, "survivor"))]
    try:
        await _event_count(log, "start", 2)
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        result = json.loads(await requests[1])
        assert "survivor" in result["result"]
        assert len(_events(log, "spawn")) == 2
        await _state(task, MCPConnectionState.IDLE)
        assert task.health_snapshot()["activeRequests"] == 0
    finally:
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.fixture(params=["auto", "legacy", "sse"])
async def http_server(tmp_path, monkeypatch, request):
    import uvicorn
    from mcp.server.mcpserver import MCPServer

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "http-home"))
    mcp = MCPServer("lifecycle-http")
    events = []

    @mcp.tool()
    async def work(label: str, delay: float) -> dict:
        events.append(("start", label))
        await asyncio.sleep(delay)
        events.append(("done", label))
        return {"label": label}

    mode = request.param
    app = mcp.sse_app() if mode == "sse" else mcp.streamable_http_app(
        stateless_http=False, json_response=True,
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(5):
            while not server.started:
                assert thread.is_alive()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/{'sse' if mode == 'sse' else 'mcp'}", mode, events
    finally:
        await asyncio.to_thread(client.shutdown_mcp_servers)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        listener.close()
        assert not thread.is_alive()
        client._reset_server_error("http-recycle")


async def test_real_http_and_sse_idle_wake_and_drain_keep_calls_exactly_once(http_server):
    from flowly.agent.tools.registry import ToolRegistry

    url, mode, events = http_server
    registry = ToolRegistry()
    names = await asyncio.to_thread(client.discover_mcp_tools, servers={"http-recycle": {
        "url": url, "protocol": "auto" if mode == "sse" else mode,
        "transport": "sse" if mode == "sse" else "http",
        "timeout": 3, "connect_timeout": 3, "elicitation": {"enabled": False},
        "lifecycle": {"idle_timeout": 0.03, "max_lifetime": 0.07},
    }}, tool_registry=registry)
    assert names == ["mcp_http_recycle_work"]
    task = client._servers["http-recycle"]
    await _state(task, MCPConnectionState.IDLE)
    first = asyncio.create_task(registry.execute("mcp_http_recycle_work", {
        "label": "first", "delay": 0.2,
    }))
    try:
        async with asyncio.timeout(3):
            while ("start", "first") not in events:
                await asyncio.sleep(0.005)
        await _state(task, MCPConnectionState.DRAINING)
        assert task.health_snapshot()["activeRequests"] == 1
        second = asyncio.create_task(registry.execute("mcp_http_recycle_work", {
            "label": "second", "delay": 0,
        }))
        results = await asyncio.gather(first, second)
        assert all("error" not in json.loads(result) for result in results), results
        assert events == [("start", "first"), ("done", "first"),
                          ("start", "second"), ("done", "second")]
        assert task._connection_generation == 3
        await _state(task, MCPConnectionState.IDLE)
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_real_http_and_sse_can_load_manifest_without_connecting(http_server):
    from flowly.agent.tools.registry import ToolRegistry

    url, mode, events = http_server
    cfg = {"url": url, "protocol": "auto" if mode == "sse" else mode,
           "transport": "sse" if mode == "sse" else "http",
           "timeout": 3, "connect_timeout": 3, "lifecycle": {"lazy_start": True}}
    for source in ("live", "manifest"):
        registry = ToolRegistry()
        assert await asyncio.to_thread(client.discover_mcp_tools,
            servers={"http-recycle": cfg}, tool_registry=registry) == ["mcp_http_recycle_work"]
        health = client.get_mcp_server_health()["http-recycle"]
        assert health["catalogSource"] == source
        if source == "manifest":
            assert not health["connected"]
            result = json.loads(await registry.execute("mcp_http_recycle_work", {"label": "fresh", "delay": 0}))
            assert "error" not in result
            assert events == [("start", "fresh"), ("done", "fresh")]
        await asyncio.to_thread(client.shutdown_mcp_servers)


def _remote(**changes):
    values = {"name": "read", "description": "Read data", "inputSchema": {"type": "object"}}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("changes", [
    {"annotations": {"readOnlyHint": False}},
    {"outputSchema": {"type": "array"}},
    {"description": "New semantics"},
    {"inputSchema": {"type": "object", "required": ["account"]}},
    {"execution": {"taskSupport": "required"}},
])
async def test_contract_change_rejects_old_wrapper_without_call_or_breaker(monkeypatch, changes):
    from flowly.mcp.tool import MCPTool

    async with _server(monkeypatch, idle=0) as (task, _, _):
        monkeypatch.setattr(client, "get_mcp_loop", asyncio.get_running_loop)
        task.tools = [_remote(annotations={"readOnlyHint": True})]
        old = MCPTool(server_task=task, remote_tool=task.tools[0])
        task.tools = [_remote(annotations={"readOnlyHint": True}, **{
            key: value for key, value in changes.items() if key != "annotations"
        })]
        if "annotations" in changes:
            task.tools = [_remote(**changes)]
        result = json.loads(await old.execute())
        assert "changed" in result["error"]
        assert not client._server_error_counts.get(task.name)
