"""Tests for tools/list_changed re-registration logic (D8).

Driving a real server to emit ``tools/list_changed`` mid-session is
flaky, so we test the re-registration core (``_reregister_server_tools``)
directly: it must register newly-appeared tools, deregister vanished
ones, and leave unchanged tools alone.
"""

from __future__ import annotations

from types import SimpleNamespace

import flowly.mcp.client as client


class _Registry:
    def __init__(self):
        self.tools = {}

    def has(self, name):
        return name in self.tools

    def register(self, tool):
        self.tools[tool.name] = tool

    def unregister(self, name):
        self.tools.pop(name, None)


def _remote(name: str, *, description: str | None = None, schema=None):
    return SimpleNamespace(
        name=name,
        description=description or f"{name} desc",
        inputSchema=schema,
    )


def _make_server_task(registry, tools, registered_names):
    task = client.MCPServerTask("srv")
    task.tools = tools
    task.capabilities = None  # don't register utility tools
    task._registry = registry
    task._server_cfg = {"tools": {}}
    task._registered_names = list(registered_names)
    return task


def test_new_tool_added_on_refresh():
    reg = _Registry()
    # Start with one tool already registered.
    task = _make_server_task(reg, [_remote("alpha")], [])
    client._register_tools_for_server(
        server_task=task, server_cfg=task._server_cfg, tool_registry=reg,
    )
    assert "mcp_srv_alpha" in reg.tools

    # Server now reports two tools.
    task.tools = [_remote("alpha"), _remote("beta")]
    client._reregister_server_tools(task)
    assert "mcp_srv_alpha" in reg.tools
    assert "mcp_srv_beta" in reg.tools


def test_removed_tool_deregistered_on_refresh():
    reg = _Registry()
    task = _make_server_task(reg, [_remote("alpha"), _remote("beta")], [])
    client._register_tools_for_server(
        server_task=task, server_cfg=task._server_cfg, tool_registry=reg,
    )
    assert {"mcp_srv_alpha", "mcp_srv_beta"} <= set(reg.tools)

    # beta vanished.
    task.tools = [_remote("alpha")]
    client._reregister_server_tools(task)
    assert "mcp_srv_alpha" in reg.tools
    assert "mcp_srv_beta" not in reg.tools


def test_unchanged_tool_kept_in_place():
    reg = _Registry()
    task = _make_server_task(reg, [_remote("alpha")], [])
    client._register_tools_for_server(
        server_task=task, server_cfg=task._server_cfg, tool_registry=reg,
    )
    original = reg.tools["mcp_srv_alpha"]

    # Same tool list — refresh must not churn the entry.
    client._reregister_server_tools(task)
    assert reg.tools["mcp_srv_alpha"] is original


def test_sanitized_name_collision_keeps_first_remote_identity_consistently():
    from flowly.mcp.tool import MCPTool

    reg = _Registry()
    task = _make_server_task(reg, [_remote("a-b", description="Same"), _remote("a_b", description="Same")], [])
    first = MCPTool(server_task=task, remote_tool=task.tools[0])
    second = MCPTool(server_task=task, remote_tool=task.tools[1])
    assert first.to_schema() == second.to_schema()
    assert first.contract_fingerprint() != second.contract_fingerprint()
    client._register_tools_for_server(server_task=task, server_cfg={}, tool_registry=reg)
    original = reg.tools["mcp_srv_a_b"]
    client._reregister_server_tools(task)
    assert reg.tools["mcp_srv_a_b"] is original
    assert original._remote_name == "a-b"
    task.tools = task.tools[1:]
    client._reregister_server_tools(task)
    assert reg.tools["mcp_srv_a_b"]._remote_name == "a_b"


def test_same_name_schema_change_replaces_tool_and_advances_generation():
    from flowly.agent.tools.registry import ToolRegistry

    reg = ToolRegistry()
    task = _make_server_task(
        reg,
        [_remote(
            "alpha",
            description="Old description",
            schema={"type": "object", "properties": {"old": {"type": "string"}}},
        )],
        [],
    )
    client._register_tools_for_server(
        server_task=task,
        server_cfg=task._server_cfg,
        tool_registry=reg,
    )
    original = reg.get("mcp_srv_alpha")
    generation = reg.generation

    task.tools = [_remote(
        "alpha",
        description="New description",
        schema={"type": "object", "properties": {"new": {"type": "integer"}}},
    )]
    client._reregister_server_tools(task)

    replacement = reg.get("mcp_srv_alpha")
    assert replacement is not original
    assert replacement is not None
    assert replacement.description == "New description"
    assert "new" in replacement.parameters["properties"]
    assert reg.generation == generation + 1


def test_refresh_never_overwrites_or_removes_a_foreign_replacement():
    reg = _Registry()
    task = _make_server_task(reg, [_remote("alpha")], [])
    client._register_tools_for_server(
        server_task=task,
        server_cfg=task._server_cfg,
        tool_registry=reg,
    )
    name = "mcp_srv_alpha"
    foreign = object()
    reg.tools[name] = foreign

    client._reregister_server_tools(task)
    assert reg.tools[name] is foreign

    task.tools = []
    client._reregister_server_tools(task)
    assert reg.tools[name] is foreign


async def test_terminal_shutdown_retires_every_owned_catalog_but_keeps_replacements():
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.mcp.tool import MCPTool

    native, custom = ToolRegistry(), _Registry()
    task = client.MCPServerTask("srv")
    task.tools = [_remote("alpha"), _remote("beta")]
    config = {"tools": {"resources": True, "prompts": True}}
    for registry in (native, custom):
        names = client._register_tools_for_server(server_task=task, server_cfg=config, tool_registry=registry)
        assert len(names) == 6
    foreign = MCPTool(server_task=client.MCPServerTask("srv"), remote_tool=_remote("alpha"))
    native.register(foreign)
    before = native.generation
    await task.shutdown()
    assert native.tool_names == [foreign.name]
    assert native.get(foreign.name) is foreign
    assert native.generation == before + 5
    assert custom.tools == {}
    assert not task._registry_bindings
    assert task._registry is None
    await task.shutdown()
    assert native.generation == before + 5


def test_concurrent_replacement_is_not_removed_by_stale_owner_cleanup(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from flowly.agent.tools.registry import ToolRegistry
    from flowly.mcp.tool import MCPTool

    registry = ToolRegistry()
    task = client.MCPServerTask("srv")
    old = MCPTool(server_task=task, remote_tool=_remote("alpha"))
    foreign = MCPTool(server_task=client.MCPServerTask("srv"), remote_tool=_remote("alpha", description="New owner"))
    registry.register(old)
    observed, replaced = threading.Event(), threading.Event()
    getter = registry.get

    def paused_get(name):
        value = getter(name)
        observed.set()
        assert replaced.wait(5)
        return value

    monkeypatch.setattr(registry, "get", paused_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client._unregister_owned_tool, registry, old.name, task)
        try:
            assert observed.wait(5)
            registry.register(foreign)
            generation = registry.generation
        finally:
            replaced.set()
        pending.result(timeout=5)
    assert getter(foreign.name) is foreign
    assert registry.generation == generation
    assert registry.get_toolsets()[foreign.name] == foreign.toolset
