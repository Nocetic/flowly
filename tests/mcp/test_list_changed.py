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


def _remote(name: str):
    return SimpleNamespace(name=name, description=f"{name} desc", inputSchema=None)


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
