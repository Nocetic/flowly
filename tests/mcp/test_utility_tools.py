"""Integration tests for resource/prompt utility tools (D9).

Spins up a FastMCP server that exposes a tool, a resource, and a
prompt, then verifies:

* With ``tools.resources``/``tools.prompts`` enabled AND the server
  advertising those capabilities, the four utility tools register.
* With the config flags off, no utility tools register even though the
  server advertises the capability.
* A tools-only server never registers utility tools regardless of
  config (capability gating).
* The utility tools actually return data from the server.

Skipped if the ``mcp`` SDK isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp SDK not installed")


_RESOURCE_SERVER = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("flowly-rp")

@mcp.tool()
def ping() -> str:
    \"\"\"Return pong.\"\"\"
    return "pong"

@mcp.resource("file://greeting")
def greeting() -> str:
    \"\"\"A friendly greeting resource.\"\"\"
    return "hello from resource"

@mcp.prompt()
def summarize(topic: str) -> str:
    \"\"\"Prompt template that summarizes a topic.\"\"\"
    return f"Please summarize: {topic}"

if __name__ == "__main__":
    mcp.run()
"""

class _Registry:
    def __init__(self):
        self.tools = {}

    def has(self, name):
        return name in self.tools

    def register(self, tool):
        self.tools[tool.name] = tool

    def unregister(self, name):
        self.tools.pop(name, None)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly"))
    (tmp_path / "flowly").mkdir(parents=True, exist_ok=True)
    return tmp_path / "flowly"


@pytest.fixture(autouse=True)
def reset_mcp():
    yield
    from flowly.mcp import shutdown_mcp_servers
    try:
        shutdown_mcp_servers()
    except Exception:
        pass


def _write(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / name
    p.write_text(source)
    return p


def _cfg(script: Path, *, resources=False, prompts=False) -> dict:
    return {
        "enabled": True,
        "command": sys.executable,
        "args": [str(script)],
        "env": {},
        "url": "",
        "headers": {},
        "timeout": 15,
        "connect_timeout": 10,
        "tools": {
            "include": [],
            "exclude": [],
            "resources": resources,
            "prompts": prompts,
        },
    }


def test_utility_tools_register_when_enabled(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = _write(tmp_path, "rp.py", _RESOURCE_SERVER)
    reg = _Registry()
    names = discover_mcp_tools(
        servers={"rp": _cfg(script, resources=True, prompts=True)},
        tool_registry=reg,
    )
    assert "mcp_rp_ping" in names
    assert "mcp_rp_list_resources" in names
    assert "mcp_rp_read_resource" in names
    assert "mcp_rp_list_prompts" in names
    assert "mcp_rp_get_prompt" in names


def test_utility_tools_absent_when_config_off(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = _write(tmp_path, "rp.py", _RESOURCE_SERVER)
    reg = _Registry()
    names = discover_mcp_tools(
        servers={"rp": _cfg(script, resources=False, prompts=False)},
        tool_registry=reg,
    )
    assert "mcp_rp_ping" in names
    assert not any("list_resources" in n or "list_prompts" in n for n in names)


def test_capability_gate_drops_unadvertised(monkeypatch):
    """Unit test: a server that advertises NO resources/prompts capability
    gets its utility tools gated out even when config asks for them.

    We exercise the gate directly rather than via FastMCP, because
    FastMCP always advertises (and implements) empty resource/prompt
    capabilities — it can't model a genuinely tools-only server like the
    spec-compliant Context7 (which advertises only ``tools``).
    """
    from types import SimpleNamespace
    from flowly.mcp.client import _utility_tools_for_server, MCPServerTask

    task = MCPServerTask("ctx7")
    task.tool_timeout = 10.0
    # Spec-compliant tools-only server: resources/prompts are None.
    task.capabilities = SimpleNamespace(tools=object(), resources=None, prompts=None)

    cfg = {"tools": {"resources": True, "prompts": True}}
    utils = _utility_tools_for_server(task, cfg)
    assert utils == []


def test_capability_gate_allows_advertised():
    from types import SimpleNamespace
    from flowly.mcp.client import _utility_tools_for_server, MCPServerTask

    task = MCPServerTask("srv")
    task.tool_timeout = 10.0
    task.capabilities = SimpleNamespace(
        tools=object(), resources=object(), prompts=object(),
    )
    cfg = {"tools": {"resources": True, "prompts": True}}
    utils = _utility_tools_for_server(task, cfg)
    names = {u.name for u in utils}
    assert names == {
        "mcp_srv_list_resources",
        "mcp_srv_read_resource",
        "mcp_srv_list_prompts",
        "mcp_srv_get_prompt",
    }


def test_list_resources_returns_data(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = _write(tmp_path, "rp.py", _RESOURCE_SERVER)
    reg = _Registry()
    discover_mcp_tools(
        servers={"rp": _cfg(script, resources=True, prompts=True)},
        tool_registry=reg,
    )
    tool = reg.tools["mcp_rp_list_resources"]
    result = json.loads(asyncio.run(tool.execute()))
    assert "resources" in result
    uris = [r.get("uri", "") for r in result["resources"]]
    assert any("greeting" in u for u in uris)


def test_list_prompts_returns_data(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = _write(tmp_path, "rp.py", _RESOURCE_SERVER)
    reg = _Registry()
    discover_mcp_tools(
        servers={"rp": _cfg(script, resources=True, prompts=True)},
        tool_registry=reg,
    )
    tool = reg.tools["mcp_rp_list_prompts"]
    result = json.loads(asyncio.run(tool.execute()))
    assert "prompts" in result
    names = [p.get("name", "") for p in result["prompts"]]
    assert "summarize" in names
