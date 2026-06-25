"""`flowly update` — install-mode detection and the right upgrade path.

The keystone is the **managed** mode: when Flowly runs as the Nuitka-compiled
binary embedded in Flowly Desktop, self-update is a no-op (the desktop app's
own auto-updater owns the binary). Every other mode (uv-tool, pipx, pip,
source) maps to its native upgrade command.
"""

import sys

import pytest

from flowly.cli import update_cmd


def test_managed_mode_when_compiled(monkeypatch):
    # Simulate Nuitka: __main__ carries a __compiled__ attribute.
    fake_main = type(sys)("__main__")
    fake_main.__compiled__ = object()
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    assert update_cmd.is_managed_binary() is True
    assert update_cmd.detect_install_mode() == "managed"


def test_managed_mode_when_sys_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert update_cmd.is_managed_binary() is True


def test_uv_tool_mode(monkeypatch):
    monkeypatch.setattr(update_cmd, "is_managed_binary", lambda: False)
    monkeypatch.setattr(sys, "prefix", "/home/u/.local/share/uv/tools/flowly-ai")
    monkeypatch.setattr(update_cmd, "_is_source_checkout", lambda: False)
    assert update_cmd.detect_install_mode() == "uv-tool"


def test_pipx_mode(monkeypatch):
    monkeypatch.setattr(update_cmd, "is_managed_binary", lambda: False)
    monkeypatch.setattr(sys, "prefix", "/home/u/.local/pipx/venvs/flowly-ai")
    monkeypatch.setattr(update_cmd, "_is_source_checkout", lambda: False)
    assert update_cmd.detect_install_mode() == "pipx"


def test_source_mode(monkeypatch):
    monkeypatch.setattr(update_cmd, "is_managed_binary", lambda: False)
    monkeypatch.setattr(update_cmd, "_is_source_checkout", lambda: True)
    assert update_cmd.detect_install_mode() == "source"


def test_pip_fallback(monkeypatch):
    monkeypatch.setattr(update_cmd, "is_managed_binary", lambda: False)
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(update_cmd, "_is_source_checkout", lambda: False)
    assert update_cmd.detect_install_mode() == "pip"


@pytest.mark.parametrize(
    "mode,expect_head",
    [
        ("uv-tool", ["uv", "tool", "upgrade"]),
        ("pipx", ["pipx", "upgrade"]),
        ("pip", [sys.executable, "-m", "pip", "install", "--upgrade"]),
    ],
)
def test_upgrade_command_per_mode(mode, expect_head):
    cmd = update_cmd.upgrade_command(mode)
    assert cmd[: len(expect_head)] == expect_head
    assert cmd[-1] == "flowly-ai" or "flowly-ai" in cmd


def test_managed_has_no_upgrade_command():
    assert update_cmd.upgrade_command("managed") is None
    assert update_cmd.upgrade_command("source") is None


def test_version_newer():
    assert update_cmd._is_newer("1.2.0", "1.1.9") is True
    assert update_cmd._is_newer("1.1.0", "1.1.0") is False
    assert update_cmd._is_newer("1.0.0", "1.2.0") is False
    # Non-numeric / dev suffixes don't crash.
    assert update_cmd._is_newer("2.1.0", "2.1.0-dev") in (True, False)
