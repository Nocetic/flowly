"""`flowly update` — install-mode detection and the right update path.

Two modes do real work: **managed** (inside Flowly Desktop) is a no-op —
the app owns its binary — and **source** (the git checkout every current
install is) pulls + reinstalls. The legacy packaged modes (uv-tool, pipx,
pip: the old PyPI package) no longer receive releases, so `update` tells
them the truth and points at the install-script migration instead of
querying PyPI forever in vain.
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


@pytest.mark.parametrize("mode", ["uv-tool", "pipx", "pip"])
def test_legacy_packaged_mode_points_at_migration(mode, monkeypatch, capsys):
    """A legacy PyPI-era install gets the truth + the migration one-liner,
    never a doomed PyPI check that reports "up to date" forever."""
    monkeypatch.setattr(update_cmd, "detect_install_mode", lambda: mode)
    rc = update_cmd.run_update(check_only=False, force=False, restart=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "legacy packaged install" in out
    assert "install.sh" in out or "install.ps1" in out


def test_legacy_packaged_check_also_says_so(monkeypatch, capsys):
    monkeypatch.setattr(update_cmd, "detect_install_mode", lambda: "uv-tool")
    rc = update_cmd.run_update(check_only=True, force=False, restart=False)
    assert rc == 1
    assert "legacy packaged install" in capsys.readouterr().out


def test_version_newer():
    assert update_cmd._is_newer("1.2.0", "1.1.9") is True
    assert update_cmd._is_newer("1.1.0", "1.1.0") is False
    assert update_cmd._is_newer("1.0.0", "1.2.0") is False
    # Non-numeric / dev suffixes don't crash.
    assert update_cmd._is_newer("2.1.0", "2.1.0-dev") in (True, False)
