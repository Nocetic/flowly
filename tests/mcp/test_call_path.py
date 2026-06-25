"""End-to-end tests for the MCP tool-call path (breaker + image, Faz 2a).

Uses a real FastMCP stdio server so the circuit-breaker accounting and
image-content caching are exercised through the production call path,
not just unit-mocked.

Skipped if the ``mcp`` SDK isn't installed.
"""

from __future__ import annotations

import asyncio
import base64
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
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
import base64

mcp = FastMCP("flowly-img")

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
    yield
    from flowly.mcp import shutdown_mcp_servers
    try:
        shutdown_mcp_servers()
    except Exception:
        pass
    client._server_error_counts.clear()
    client._server_breaker_opened_at.clear()


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


def test_breaker_opens_after_repeated_failures(tmp_path, isolated_home):
    import flowly.mcp.client as client

    reg = _discover(tmp_path, isolated_home)
    boom = reg.tools["mcp_img_boom"]

    # Drive failures up to threshold.
    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD):
        out = json.loads(asyncio.run(boom.execute()))
        assert "error" in out

    # Next call is short-circuited by the open breaker.
    out = json.loads(asyncio.run(boom.execute()))
    assert "unreachable" in out["error"]


def test_breaker_resets_on_success(tmp_path, isolated_home):
    import flowly.mcp.client as client

    reg = _discover(tmp_path, isolated_home)
    boom = reg.tools["mcp_img_boom"]
    shot = reg.tools["mcp_img_shot"]

    # A few failures (below threshold), then a success must reset.
    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD - 1):
        asyncio.run(boom.execute())
    assert client._server_error_counts.get("img", 0) > 0

    asyncio.run(shot.execute())
    assert client._server_error_counts.get("img", 0) == 0


def test_legit_error_keyed_data_does_not_trip_breaker(tmp_path, isolated_home):
    # A tool returning data that contains an 'error' key is a HEALTHY
    # call — our envelope wraps it under "result", so it must not bump
    # the breaker (Fix 4: only an exact {"error": ...} envelope counts).
    import flowly.mcp.client as client

    reg = _discover(tmp_path, isolated_home)
    errorish = reg.tools["mcp_img_errorish"]

    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD + 2):
        out = json.loads(asyncio.run(errorish.execute()))
        # The error-keyed payload comes back as tool data, not a failure.
        assert "result" in out
    assert client._server_error_counts.get("img", 0) == 0
