"""Stale service-unit executable detection.

The migration trap: a gateway service installed by a previous (PyPI/uv-tool)
install has that install's binary baked into its unit. After the installer
retires the old install, `systemctl restart` reports ok while the gateway
never binds its port. `_stale_exec_hint` is what turns that silence into a
pointed "reinstall the service" message.
"""

from __future__ import annotations

import plistlib

import pytest

from flowly.integrations import service_control

LABEL = "ai.flowly.gateway"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_unit(home, exec_line: str) -> None:
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{LABEL}.service").write_text(
        f"[Unit]\nDescription=Flowly\n\n[Service]\nExecStart={exec_line}\n",
        encoding="utf-8",
    )


def _write_plist(home, program_args: list[str]) -> None:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    with (agents / f"{LABEL}.plist").open("wb") as f:
        plistlib.dump({"Label": LABEL, "ProgramArguments": program_args}, f)


# ── Linux (systemd ExecStart) ────────────────────────────────────────────────

def test_linux_stale_exec_yields_hint(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    _write_unit(home, "/gone/uv-tools/flowly gateway --port 18790")
    hint = service_control._stale_exec_hint(LABEL)
    assert "/gone/uv-tools/flowly" in hint
    assert "flowly service install --start" in hint


def test_linux_healthy_exec_yields_no_hint(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    exe = home / "flowly"
    exe.write_text("#!/bin/sh\n")
    _write_unit(home, f"{exe} gateway --port 18790")
    assert service_control._stale_exec_hint(LABEL) == ""


def test_linux_exec_path_with_spaces_parses(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    _write_unit(home, '"/opt/my apps/flowly" gateway')
    assert str(service_control._unit_exec_path(LABEL)) == "/opt/my apps/flowly"


def test_no_unit_yields_no_hint(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    assert service_control._unit_exec_path(LABEL) is None
    assert service_control._stale_exec_hint(LABEL) == ""


# ── macOS (launchd plist) ────────────────────────────────────────────────────

def test_darwin_stale_exec_yields_hint(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    _write_plist(home, ["/gone/flowly", "gateway", "--port", "18790"])
    hint = service_control._stale_exec_hint(LABEL)
    assert "/gone/flowly" in hint
    assert "flowly service install --start" in hint


def test_darwin_healthy_exec_yields_no_hint(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    exe = home / "flowly"
    exe.write_text("#!/bin/sh\n")
    _write_plist(home, [str(exe), "gateway"])
    assert service_control._stale_exec_hint(LABEL) == ""


# ── Other platforms: never a false positive ──────────────────────────────────

def test_windows_yields_no_hint(home, monkeypatch):
    monkeypatch.setattr(service_control.platform, "system", lambda: "Windows")
    assert service_control._stale_exec_hint(LABEL) == ""
