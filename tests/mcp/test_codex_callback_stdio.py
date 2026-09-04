"""Real stdio interoperability for the coding-agent callback server."""

from __future__ import annotations

import asyncio
import json
import sys

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
    yield
    from flowly.mcp import shutdown_mcp_servers

    shutdown_mcp_servers()


def test_current_client_discovers_and_calls_callback() -> None:
    from flowly.mcp import discover_mcp_tools, get_mcp_server_health

    registry = _Registry()
    names = discover_mcp_tools(
        servers={
            "callback": {
                "command": sys.executable,
                "args": ["-m", "flowly.codex.tools_mcp_server"],
                "protocol": "auto",
                "transport": "stdio",
                "timeout": 10,
                "connect_timeout": 10,
                "osv_check": False,
            },
        },
        tool_registry=registry,
    )

    assert "mcp_callback_skills_list" in names
    health = get_mcp_server_health()["callback"]
    assert health["protocolEra"] == "modern"
    assert health["protocolVersion"] == "2026-07-28"

    result = json.loads(asyncio.run(registry.tools["mcp_callback_skills_list"].execute()))
    assert "result" in result
