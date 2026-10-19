"""A named agent reaches the Desktop app the same way the main agent does.

Flowly Desktop writes its local API discovery file (``electron-api.json``,
port + token) once, into the canonical home (``~/.flowly``). A named agent
runs with ``FLOWLY_HOME`` set to its own profile directory, so looking the
file up there missed it: every helper-only computer action (``list_apps``,
``list_windows``, ``read_window_state`` …) fell through to tiers that do not
know it and came back as ``Unknown action``, and ``window_list`` fell to an
``osascript`` run without the Desktop's Automation grant (``-10004``).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import flowly.profile as profile
from flowly.agent.tools import computer as computer_mod
from flowly.agent.tools import screenshot as screenshot_mod
from flowly.channels import _imessage_send as imessage_mod
from flowly.config.schema import ComputerConfig


@pytest.fixture
def named_agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The canonical home holds the Desktop bridge; the agent runs in its profile."""
    canonical = tmp_path / ".flowly"
    agent_home = canonical / "profiles" / "coding"
    agent_home.mkdir(parents=True)
    (canonical / "electron-api.json").write_text(json.dumps({"port": 43117, "token": "bridge-token"}))
    monkeypatch.setattr(profile, "_DEFAULT_HOME", canonical)
    monkeypatch.setenv("FLOWLY_HOME", str(agent_home))
    return canonical


def test_every_desktop_bridge_reader_looks_in_the_canonical_home(named_agent_home: Path) -> None:
    expected = named_agent_home / "electron-api.json"
    assert computer_mod._electron_api_file() == expected
    assert screenshot_mod._electron_api_file() == expected
    assert imessage_mod._electron_api_file() == expected


@pytest.fixture
def desktop_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> computer_mod.ComputerTool:
    """macOS with no Desktop bridge: only the in-process fallback tier is left."""
    canonical = tmp_path / ".flowly"
    canonical.mkdir()
    monkeypatch.setattr(profile, "_DEFAULT_HOME", canonical)
    monkeypatch.setattr(computer_mod, "_PLATFORM", "darwin")
    return computer_mod.ComputerTool(config=ComputerConfig(enabled=True))


async def test_a_helper_only_action_says_it_needs_the_desktop_app(desktop_closed) -> None:
    result = json.loads(await desktop_closed.execute(action="list_apps"))
    assert result["error_kind"] == "HELPER_NOT_AVAILABLE"
    assert "Unknown action" not in result["error"]
    assert "Flowly Desktop" in result["error"]


async def test_an_unknown_action_lists_the_valid_ones(desktop_closed) -> None:
    result = json.loads(await desktop_closed.execute(action="list_every_window"))
    assert result["error_kind"] == "INVALID_PARAMS"
    assert result["error"].startswith("Unknown action: list_every_window. Valid actions: ")
    for valid in ("list_windows", "list_apps", "window_list", "read_window_state"):
        assert valid in result["error"]


@pytest.mark.parametrize("stderr", [
    "40:99: execution error: System Events got an error: A privilege violation occurred. (-10004)",
    "execution error: Not authorized to send Apple events to System Events. (-1743)",
])
async def test_a_blocked_apple_event_says_where_to_allow_it(
    desktop_closed, monkeypatch: pytest.MonkeyPatch, stderr: str,
) -> None:
    import subprocess

    monkeypatch.setattr(desktop_closed, "_run", lambda cmd, timeout=15: subprocess.CompletedProcess(cmd, 1, "", stderr))
    result = json.loads(await desktop_closed.execute(action="window_list"))
    assert result["error_kind"] == "AUTOMATION_PERMISSION_DENIED"
    assert "System Settings → Privacy & Security → Automation" in result["error"]
    assert result["retryable"] is False
    assert result["detail"] == stderr


async def test_a_blocked_keystroke_says_where_to_allow_accessibility(
    desktop_closed, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    stderr = "execution error: System Events got an error: osascript is not allowed assistive access. (-25211)"
    monkeypatch.setattr(desktop_closed, "_run", lambda cmd, timeout=15: subprocess.CompletedProcess(cmd, 1, "", stderr))
    monkeypatch.setattr(desktop_closed, "_delay", lambda: None)
    result = json.loads(await desktop_closed.execute(action="type", text="hello"))
    assert result["error_kind"] == "ACCESSIBILITY_PERMISSION_DENIED"
    assert "System Settings → Privacy & Security → Accessibility" in result["error"]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def test_a_named_agent_reaches_the_native_helper(
    named_agent_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data)
        calls.append((request.full_url, request.headers.get("Authorization"), body))
        return _Response(json.dumps({"success": True, "result": {"apps": [{"name": "Finder"}]}}).encode())

    monkeypatch.setattr(computer_mod, "_PLATFORM", "darwin")
    monkeypatch.setattr(computer_mod.urllib.request, "urlopen", fake_urlopen)
    tool = computer_mod.ComputerTool(config=ComputerConfig(enabled=True))

    result = json.loads(await tool.execute(action="list_apps"))

    assert "error" not in result, result
    assert result["apps"] == [{"name": "Finder"}]
    assert calls == [("http://127.0.0.1:43117/helper", "Bearer bridge-token", {"method": "list_apps", "params": {}})]
