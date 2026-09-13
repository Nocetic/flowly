"""End-to-end tests for the MCP tool-call path (breaker + image, Faz 2a).

Uses a real FastMCP stdio server so the circuit-breaker accounting and
image-content caching are exercised through the production call path,
not just unit-mocked.

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


# 1x1 PNG, base64 — returned by the server's `shot` tool as an image block.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_IMAGE_SERVER = f"""
from mcp.server.mcpserver import Image, MCPServer
import asyncio
import base64
import time

mcp = MCPServer("flowly-img")

@mcp.tool()
def shot() -> Image:
    \"\"\"Return a tiny PNG image.\"\"\"
    return Image(data=base64.b64decode("{_PNG_B64}"), format="png")

@mcp.tool()
def boom() -> str:
    \"\"\"Always raises so we can exercise the circuit breaker.\"\"\"
    raise RuntimeError("intentional failure")

@mcp.tool()
def errorish() -> dict:
    \"\"\"Return legit data that happens to contain an 'error' key.\"\"\"
    return {{"error": "this is data, not a failure", "ok": True}}

@mcp.tool()
async def timed(label: str) -> dict:
    \"\"\"Return a monotonic interval after a short asynchronous wait.\"\"\"
    started = time.monotonic()
    await asyncio.sleep(0.15)
    return {{"label": label, "started": started, "ended": time.monotonic()}}

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
    import flowly.mcp.client as client
    client._server_error_counts.clear()
    client._server_breaker_opened_at.clear()
    client._server_breaker_probe_inflight.clear()
    yield
    from flowly.mcp import shutdown_mcp_servers
    try:
        shutdown_mcp_servers()
    except Exception:
        pass
    client._server_error_counts.clear()
    client._server_breaker_opened_at.clear()
    client._server_breaker_probe_inflight.clear()


def _cfg(script: Path) -> dict:
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


def _discover(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = tmp_path / "img.py"
    script.write_text(_IMAGE_SERVER)
    reg = _Registry()
    discover_mcp_tools(servers={"img": _cfg(script)}, tool_registry=reg)
    return reg


def test_image_result_emits_media_tag(tmp_path, isolated_home):
    reg = _discover(tmp_path, isolated_home)
    tool = reg.tools["mcp_img_shot"]
    result = json.loads(asyncio.run(tool.execute()))
    payload = json.dumps(result)
    assert "MEDIA:" in payload
    # The cached file exists under $FLOWLY_HOME/media/mcp/.
    media_dir = isolated_home / "media" / "mcp"
    cached = list(media_dir.glob("mcp-*.png"))
    assert cached, "expected a cached PNG under media/mcp/"


def test_tool_errors_do_not_open_connection_breaker(tmp_path, isolated_home):
    import flowly.mcp.client as client

    reg = _discover(tmp_path, isolated_home)
    boom = reg.tools["mcp_img_boom"]

    # The peer replies with isError=True, so it is not unreachable.
    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD + 1):
        out = json.loads(asyncio.run(boom.execute()))
        assert "error" in out
        assert out["isError"] is True
    assert client.circuit_breaker_block_reason("img") is None


def test_breaker_resets_on_success(tmp_path, isolated_home):
    import flowly.mcp.client as client

    reg = _discover(tmp_path, isolated_home)
    shot = reg.tools["mcp_img_shot"]

    # Connection failures (below threshold), then a real response resets.
    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD - 1):
        client._bump_server_error("img")
    assert client._server_error_counts.get("img", 0) > 0

    asyncio.run(shot.execute())
    assert client._server_error_counts.get("img", 0) == 0


def test_legit_error_keyed_data_does_not_trip_breaker(tmp_path, isolated_home):
    # A tool returning data that contains an 'error' key is a HEALTHY
    # call — our envelope wraps it under "result", preserving the data
    # without treating it as a failed operation or connection failure.
    import flowly.mcp.client as client

    reg = _discover(tmp_path, isolated_home)
    errorish = reg.tools["mcp_img_errorish"]

    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD + 2):
        out = json.loads(asyncio.run(errorish.execute()))
        # The error-keyed payload comes back as tool data, not a failure.
        assert "result" in out
    assert client._server_error_counts.get("img", 0) == 0


def test_auto_mode_negotiates_current_stateless_protocol(tmp_path, isolated_home):
    reg = _discover(tmp_path, isolated_home)
    server_task = reg.tools["mcp_img_shot"]._server_task
    health = server_task.health_snapshot()

    assert health["protocolMode"] == "auto"
    assert health["protocolEra"] == "modern"
    assert health["protocolVersion"] == "2026-07-28"


def test_explicit_legacy_mode_preserves_older_servers(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = tmp_path / "legacy.py"
    script.write_text(_IMAGE_SERVER)
    config = _cfg(script)
    config["protocol"] = "legacy"
    reg = _Registry()

    discover_mcp_tools(servers={"legacy": config}, tool_registry=reg)

    server_task = reg.tools["mcp_legacy_shot"]._server_task
    health = server_task.health_snapshot()
    assert health["protocolMode"] == "legacy"
    assert health["protocolEra"] == "legacy"
    assert health["protocolVersion"] == "2025-11-25"


def test_strict_stateless_mode_requires_modern_discovery(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = tmp_path / "stateless.py"
    script.write_text(_IMAGE_SERVER)
    config = _cfg(script)
    config["protocol"] = "stateless"
    reg = _Registry()

    discover_mcp_tools(servers={"stateless": config}, tool_registry=reg)

    server_task = reg.tools["mcp_stateless_shot"]._server_task
    health = server_task.health_snapshot()
    assert health["protocolMode"] == "stateless"
    assert health["protocolEra"] == "modern"
    assert health["protocolVersion"] == "2026-07-28"


def test_parallel_server_calls_overlap_on_the_wire(tmp_path, isolated_home):
    from flowly.mcp import discover_mcp_tools

    script = tmp_path / "parallel.py"
    script.write_text(_IMAGE_SERVER)
    config = _cfg(script)
    config["supports_parallel_tool_calls"] = True
    config["max_parallel_tool_calls"] = 2
    registry = _Registry()
    discover_mcp_tools(servers={"parallel": config}, tool_registry=registry)
    tool = registry.tools["mcp_parallel_timed"]

    async def _run():
        return await asyncio.gather(tool.execute(label="a"), tool.execute(label="b"))

    raw_a, raw_b = asyncio.run(_run())
    def _interval(raw):
        value = json.loads(raw)["result"]
        return json.loads(value) if isinstance(value, str) else value

    interval_a = _interval(raw_a)
    interval_b = _interval(raw_b)
    assert max(interval_a["started"], interval_b["started"]) < min(
        interval_a["ended"], interval_b["ended"]
    )
    health = tool._server_task.health_snapshot()
    assert health["parallelToolCalls"] is True
    assert health["peakInflightToolCalls"] == 2
