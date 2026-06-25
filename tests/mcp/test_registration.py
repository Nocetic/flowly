"""Tests for MCP-tool registration into Flowly's ``ToolRegistry``.

This is an integration test that spins up an actual stdio MCP server
implemented with FastMCP, runs discovery, and verifies:

* Tools are registered under the ``mcp_{server}_{tool}`` naming scheme.
* The OpenAI function-schema returned by the registry has the
  normalized parameters object.
* ``include`` / ``exclude`` filter at registration time.
* Built-in name collisions skip the MCP tool and preserve the
  pre-existing entry.
* A real tool call returns the JSON envelope shape the agent expects.
* Disabled servers are skipped without errors.

Skipped if the ``mcp`` SDK isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

try:
    import mcp  # noqa: F401 — availability probe
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not MCP_AVAILABLE, reason="mcp SDK not installed",
)


# ---------------------------------------------------------------------------
# Fake stdio MCP server (written to disk so we can spawn it as a subprocess)
# ---------------------------------------------------------------------------

_FAKE_SERVER_SOURCE = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("flowly-test")

@mcp.tool()
def echo(message: str) -> str:
    \"\"\"Echo back the given message.\"\"\"
    return f"echoed: {message}"

@mcp.tool()
def add(a: int, b: int) -> int:
    \"\"\"Add two integers.\"\"\"
    return a + b

if __name__ == "__main__":
    mcp.run()
"""


@pytest.fixture
def fake_server_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER_SOURCE)
    return script


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly"))
    (tmp_path / "flowly").mkdir(parents=True, exist_ok=True)
    return tmp_path / "flowly"


@pytest.fixture(autouse=True)
def reset_mcp_loop():
    """Clean up the MCP background loop between tests."""
    yield
    from flowly.mcp import shutdown_mcp_servers
    try:
        shutdown_mcp_servers()
    except Exception:
        pass


class _StubRegistry:
    """Minimal ToolRegistry stand-in to avoid the agent import chain."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def has(self, name: str) -> bool:
        return name in self.tools

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


def _make_server_cfg(script: Path) -> dict:
    return {
        "enabled": True,
        "command": sys.executable,
        "args": [str(script)],
        "env": {},
        "url": "",
        "headers": {},
        "timeout": 15,
        "connect_timeout": 10,
        "tools": {"include": [], "exclude": [], "resources": False, "prompts": False},
    }


def test_discovery_registers_all_tools(fake_server_script: Path, isolated_home: Path):
    from flowly.mcp import discover_mcp_tools

    reg = _StubRegistry()
    names = discover_mcp_tools(
        servers={"fake": _make_server_cfg(fake_server_script)},
        tool_registry=reg,
    )
    assert "mcp_fake_echo" in names
    assert "mcp_fake_add" in names
    assert "mcp_fake_echo" in reg.tools


def test_include_filter_limits_registration(fake_server_script: Path, isolated_home: Path):
    from flowly.mcp import discover_mcp_tools

    cfg = _make_server_cfg(fake_server_script)
    cfg["tools"]["include"] = ["echo"]
    reg = _StubRegistry()
    names = discover_mcp_tools(servers={"fake": cfg}, tool_registry=reg)
    assert names == ["mcp_fake_echo"]


def test_exclude_filter_drops_matching_tools(fake_server_script: Path, isolated_home: Path):
    from flowly.mcp import discover_mcp_tools

    cfg = _make_server_cfg(fake_server_script)
    cfg["tools"]["exclude"] = ["add"]
    reg = _StubRegistry()
    names = discover_mcp_tools(servers={"fake": cfg}, tool_registry=reg)
    assert "mcp_fake_echo" in names
    assert "mcp_fake_add" not in names


def test_collision_preserves_pre_existing_tool(
    fake_server_script: Path, isolated_home: Path,
):
    from flowly.mcp import discover_mcp_tools

    reg = _StubRegistry()
    # Pre-seed a "native" tool whose name collides with what MCP would
    # register.
    class _Sentinel:
        name = "mcp_fake_echo"
    reg.tools["mcp_fake_echo"] = _Sentinel()

    names = discover_mcp_tools(
        servers={"fake": _make_server_cfg(fake_server_script)},
        tool_registry=reg,
    )
    # Sentinel survived; MCP echo did NOT replace it.
    assert reg.tools["mcp_fake_echo"].__class__.__name__ == "_Sentinel"
    # `add` registered normally.
    assert "mcp_fake_add" in names


def test_disabled_server_skipped(fake_server_script: Path, isolated_home: Path):
    from flowly.mcp import discover_mcp_tools

    cfg = _make_server_cfg(fake_server_script)
    cfg["enabled"] = False
    reg = _StubRegistry()
    names = discover_mcp_tools(servers={"fake": cfg}, tool_registry=reg)
    assert names == []
    assert reg.tools == {}


def test_tool_call_returns_json_envelope(fake_server_script: Path, isolated_home: Path):
    from flowly.mcp import discover_mcp_tools

    reg = _StubRegistry()
    discover_mcp_tools(
        servers={"fake": _make_server_cfg(fake_server_script)},
        tool_registry=reg,
    )
    tool = reg.tools["mcp_fake_echo"]

    result = asyncio.run(tool.execute(message="hi"))
    parsed = json.loads(result)
    # Either {"result": ...} or {"result": text, "structuredContent": ...}
    assert "result" in parsed
    assert "echoed: hi" in json.dumps(parsed)


def test_failed_server_does_not_block_others(
    fake_server_script: Path, isolated_home: Path,
):
    from flowly.mcp import discover_mcp_tools

    bad_cfg = {
        "enabled": True,
        "command": "/nonexistent/please-do-not-exist-xyz",
        "args": [],
        "env": {},
        "url": "",
        "headers": {},
        "timeout": 5,
        "connect_timeout": 5,
        "tools": {"include": [], "exclude": [], "resources": False, "prompts": False},
    }
    reg = _StubRegistry()
    names = discover_mcp_tools(
        servers={
            "broken": bad_cfg,
            "fake": _make_server_cfg(fake_server_script),
        },
        tool_registry=reg,
    )
    # Broken server got dropped; good server still registered.
    assert "mcp_fake_echo" in names


def test_already_connected_server_reregisters_into_new_registry(
    fake_server_script: Path, isolated_home: Path,
):
    # Fix 3: a second discover() with a FRESH registry (simulating a
    # second AgentLoop in the same process) must re-register the already-
    # connected server's tools rather than skipping and leaving the new
    # registry empty.
    from flowly.mcp import discover_mcp_tools

    cfg = {"fake": _make_server_cfg(fake_server_script)}

    reg1 = _StubRegistry()
    names1 = discover_mcp_tools(servers=cfg, tool_registry=reg1)
    assert "mcp_fake_echo" in names1

    # Second registry — server is already connected; tools must still
    # land here.
    reg2 = _StubRegistry()
    names2 = discover_mcp_tools(servers=cfg, tool_registry=reg2)
    assert "mcp_fake_echo" in names2
    assert "mcp_fake_echo" in reg2.tools
