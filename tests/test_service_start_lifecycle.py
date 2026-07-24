"""Focused service install/start lifecycle tests.

The host running this suite does not need launchd, systemd, or Task Scheduler:
platform commands are captured at the CLI boundary. The assertions cover fresh
artifacts, platform routing, and the false-success path where a manager accepts
the request but the gateway never becomes healthy.
"""

from __future__ import annotations

import io
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console

from flowly.cli import service_cmd
from flowly.integrations import service_control

LABEL = "ai.flowly.gateway"
PORT = 19876


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def output(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(
        service_cmd,
        "console",
        Console(file=stream, force_terminal=False, color_system=None),
    )
    return stream


@pytest.fixture
def install_env(tmp_path, monkeypatch, output):
    """Common side-effect isolation for direct ``service_install`` calls."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(service_cmd, "_provider_configured", lambda: True)
    monkeypatch.setattr(service_cmd, "_resolve_flowly_exec_argv", lambda: ["/opt/flowly"])
    monkeypatch.setattr(service_cmd, "_get_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(service_cmd, "_detect_public_ip", lambda: "")

    from flowly.config import loader

    config = SimpleNamespace(
        gateway=SimpleNamespace(host="127.0.0.1", token="", port=PORT),
    )
    monkeypatch.setattr(loader, "load_config", lambda: config)
    monkeypatch.setattr(loader, "save_config", lambda _config: None)
    return tmp_path


def _install(**overrides):
    args = {
        "label": LABEL,
        "port": PORT,
        "verbose": False,
        "start": True,
        "force": False,
        "persona": "",
        "cwd": "",
        "host": "",
        "remote": False,
        "token": "",
    }
    args.update(overrides)
    service_cmd.service_install(**args)


def _prepare_start(monkeypatch, *, system: str, paths):
    monkeypatch.setattr(service_cmd.platform, "system", lambda: system)
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: paths)
    monkeypatch.setattr(service_cmd, "_provider_configured", lambda: True)
    monkeypatch.setattr(service_cmd, "_port_listener_pids", lambda _port: [])
    monkeypatch.setattr(service_control, "_stale_exec_hint", lambda _label: "")
    monkeypatch.setattr(service_cmd, "_ensure_linger_linux", lambda: None)


def test_fresh_macos_install_bootstraps_user_domain_and_verifies_health(
    install_env, monkeypatch
):
    plist_path = install_env / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert not plist_path.exists()
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        service_cmd,
        "_service_paths",
        lambda _label: (plist_path, None, None),
    )
    monkeypatch.setattr(service_cmd.os, "getuid", lambda: 501)

    commands = []

    def fake_run(args, check=True):
        commands.append((args, check))
        if args[:2] == ["launchctl", "print"]:
            return _result(1)
        return _result()

    health = []
    monkeypatch.setattr(service_cmd, "_run_cmd", fake_run)
    monkeypatch.setattr(
        service_cmd,
        "_require_service_health",
        lambda port, *, manager, timeout=service_cmd.SERVICE_HEALTH_TIMEOUT: health.append(
            (port, manager)
        ),
    )

    _install()

    assert plist_path.exists()
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"][-3:] == ["gateway", "--port", str(PORT)]
    assert not any(command[:2] == ["launchctl", "bootout"] for command, _check in commands)
    assert (
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        True,
    ) in commands
    assert health == [(PORT, "launchd")]


def test_fresh_linux_install_enables_starts_and_verifies_health(
    install_env, monkeypatch
):
    unit_path = install_env / ".config" / "systemd" / "user" / f"{LABEL}.service"
    assert not unit_path.exists()
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        service_cmd,
        "_service_paths",
        lambda _label: (None, unit_path, None),
    )
    monkeypatch.setattr(service_cmd, "_ensure_linger_linux", lambda: None)

    commands = []
    monkeypatch.setattr(
        service_cmd,
        "_run_cmd",
        lambda args, check=True: commands.append(args) or _result(),
    )
    health = []
    monkeypatch.setattr(
        service_cmd,
        "_require_service_health",
        lambda port, *, manager, timeout=service_cmd.SERVICE_HEALTH_TIMEOUT: health.append(
            (port, manager)
        ),
    )

    _install()

    assert unit_path.exists()
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert ["systemctl", "--user", "enable", LABEL] in commands
    assert ["systemctl", "--user", "restart", LABEL] in commands
    assert health == [(PORT, "systemd")]


def test_fresh_windows_install_runs_task_and_verifies_health(
    install_env, monkeypatch
):
    xml_path = install_env / "local" / f"{LABEL}.xml"
    assert not xml_path.exists()
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        service_cmd,
        "_service_paths",
        lambda _label: (None, None, xml_path),
    )
    monkeypatch.setattr(service_cmd, "_is_windows_admin", lambda: False)
    monkeypatch.setattr(service_cmd.subprocess, "run", lambda *args, **kwargs: _result())

    commands = []
    monkeypatch.setattr(
        service_cmd,
        "_run_cmd",
        lambda args, check=True: commands.append(args) or _result(),
    )
    health = []
    monkeypatch.setattr(
        service_cmd,
        "_require_service_health",
        lambda port, *, manager, timeout=service_cmd.SERVICE_HEALTH_TIMEOUT: health.append(
            (port, manager)
        ),
    )

    _install()

    assert xml_path.exists()
    assert xml_path.with_suffix(".vbs").exists()
    assert ["schtasks", "/run", "/tn", LABEL] in commands
    assert health == [(PORT, "Task Scheduler")]


def test_fresh_windows_install_falls_back_without_admin_and_still_verifies_health(
    install_env, monkeypatch
):
    xml_path = install_env / "local" / f"{LABEL}.xml"
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        service_cmd,
        "_service_paths",
        lambda _label: (None, None, xml_path),
    )
    monkeypatch.setattr(service_cmd, "_is_windows_admin", lambda: False)
    monkeypatch.setattr(
        service_cmd.subprocess,
        "run",
        lambda *args, **kwargs: _result(1, stderr="Access is denied"),
    )
    launched = []
    monkeypatch.setattr(
        service_cmd.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append((args, kwargs)),
    )
    health = []
    monkeypatch.setattr(
        service_cmd,
        "_require_service_health",
        lambda port, *, manager, timeout=service_cmd.SERVICE_HEALTH_TIMEOUT: health.append(
            (port, manager)
        ),
    )

    _install()

    startup_cmd = service_cmd._windows_startup_launcher(LABEL)
    assert startup_cmd.exists()
    assert launched[0][0] == ["wscript.exe", str(xml_path.with_suffix(".vbs"))]
    assert health == [(PORT, "Startup-folder launcher")]


def test_fresh_macos_install_fails_truthfully_without_gui_launchd_domain(
    install_env, monkeypatch, output
):
    plist_path = install_env / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        service_cmd,
        "_service_paths",
        lambda _label: (plist_path, None, None),
    )

    def fake_run(args, check=True):
        if args[:2] == ["launchctl", "print"]:
            return _result(1)
        if args[:2] == ["launchctl", "bootstrap"]:
            raise RuntimeError("Could not find domain for gui session")
        return _result()

    monkeypatch.setattr(service_cmd, "_run_cmd", fake_run)

    with pytest.raises(typer.Exit) as exc:
        _install()

    assert exc.value.exit_code == 1
    text = output.getvalue()
    assert "Could not find domain for gui session" in text
    assert f"Installed launchd service: {LABEL}" not in text


@pytest.mark.parametrize(
    ("system", "manager"),
    [
        ("Darwin", "launchd"),
        ("Linux", "systemd"),
        ("Windows", "Task Scheduler"),
    ],
)
def test_start_fails_when_manager_accepts_but_gateway_stays_unhealthy(
    tmp_path, monkeypatch, output, system, manager
):
    plist_path = tmp_path / f"{LABEL}.plist"
    unit_path = tmp_path / f"{LABEL}.service"
    xml_path = tmp_path / f"{LABEL}.xml"
    plist_path.write_bytes(
        plistlib.dumps(
            {"Label": LABEL, "ProgramArguments": ["/opt/flowly", "gateway", "--port", str(PORT)]}
        )
    )
    unit_path.write_text(
        f"[Service]\nExecStart=/opt/flowly gateway --port {PORT}\n",
        encoding="utf-8",
    )
    xml_path.write_text("<Task/>", encoding="utf-16")
    xml_path.with_suffix(".vbs").write_text(
        f'sh.Run "/opt/flowly gateway --port {PORT}", 0, True',
        encoding="utf-8",
    )
    paths = (plist_path, unit_path, xml_path)
    _prepare_start(monkeypatch, system=system, paths=paths)
    monkeypatch.setattr(service_cmd.os, "getuid", lambda: 501)

    def fake_run(args, check=True):
        if args[:2] == ["launchctl", "print"]:
            return _result(1)
        if args[:2] == ["schtasks", "/query"]:
            return _result()
        return _result()

    monkeypatch.setattr(service_cmd, "_run_cmd", fake_run)
    monkeypatch.setattr(
        service_cmd,
        "_require_service_health",
        lambda port, *, manager, timeout=service_cmd.SERVICE_HEALTH_TIMEOUT: (
            _ for _ in ()
        ).throw(
            RuntimeError(f"{manager} accepted start but health failed on {port}")
        ),
    )

    with pytest.raises(typer.Exit) as exc:
        service_cmd.service_start(label=LABEL)

    assert exc.value.exit_code == 1
    text = output.getvalue()
    assert manager in text
    assert "health failed" in text
    assert str(PORT) in text
    assert f"Started service {LABEL}" not in text


def test_start_uses_installed_custom_port_not_config_default(tmp_path, monkeypatch):
    unit_path = tmp_path / f"{LABEL}.service"
    unit_path.write_text(
        f"[Service]\nExecStart=/opt/flowly gateway --port {PORT}\n",
        encoding="utf-8",
    )
    _prepare_start(
        monkeypatch,
        system="Linux",
        paths=(None, unit_path, None),
    )
    monkeypatch.setattr(service_cmd, "_run_cmd", lambda args, check=True: _result())
    verified = []
    monkeypatch.setattr(
        service_cmd,
        "_require_service_health",
        lambda port, *, manager, timeout=service_cmd.SERVICE_HEALTH_TIMEOUT: verified.append(port),
    )

    service_cmd.service_start(label=LABEL)

    assert verified == [PORT]


def test_windows_start_uses_startup_fallback_when_task_was_not_created(
    tmp_path, monkeypatch
):
    xml_path = tmp_path / "local" / f"{LABEL}.xml"
    xml_path.parent.mkdir(parents=True)
    xml_path.write_text("<Task/>", encoding="utf-16")
    xml_path.with_suffix(".vbs").write_text(
        f'sh.Run "/opt/flowly gateway --port {PORT}", 0, True',
        encoding="utf-8",
    )
    startup_cmd = tmp_path / "startup" / f"{LABEL}.cmd"
    startup_cmd.parent.mkdir(parents=True)
    startup_cmd.write_text("@echo off", encoding="utf-8")

    _prepare_start(
        monkeypatch,
        system="Windows",
        paths=(None, None, xml_path),
    )
    monkeypatch.setattr(
        service_cmd,
        "_windows_startup_launcher",
        lambda _label: startup_cmd,
    )
    monkeypatch.setattr(
        service_cmd,
        "_run_cmd",
        lambda args, check=True: (
            (_ for _ in ()).throw(FileNotFoundError("schtasks"))
            if args[:2] == ["schtasks", "/query"]
            else _result()
        ),
    )
    popen = []
    monkeypatch.setattr(
        service_cmd.subprocess,
        "Popen",
        lambda args, **kwargs: popen.append((args, kwargs)),
    )
    monkeypatch.setattr(service_cmd, "_require_service_health", lambda *args, **kwargs: None)

    service_cmd.service_start(label=LABEL)

    assert popen
    assert popen[0][0] == ["wscript.exe", str(xml_path.with_suffix(".vbs"))]


def test_occupied_non_gateway_port_is_not_reported_as_started(
    tmp_path, monkeypatch, output
):
    unit_path = tmp_path / f"{LABEL}.service"
    unit_path.write_text(
        f"[Service]\nExecStart=/opt/flowly gateway --port {PORT}\n",
        encoding="utf-8",
    )
    _prepare_start(
        monkeypatch,
        system="Linux",
        paths=(None, unit_path, None),
    )
    monkeypatch.setattr(service_cmd, "_port_listener_pids", lambda _port: [4321])
    monkeypatch.setattr(
        service_cmd,
        "_service_health",
        lambda _port: (False, "HTTP 404"),
    )

    with pytest.raises(typer.Exit) as exc:
        service_cmd.service_start(label=LABEL)

    assert exc.value.exit_code == 1
    assert "occupied by PID 4321" in output.getvalue()


def test_requested_start_without_provider_installs_but_returns_failure(
    install_env, monkeypatch, output
):
    unit_path = install_env / f"{LABEL}.service"
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        service_cmd,
        "_service_paths",
        lambda _label: (None, unit_path, None),
    )
    monkeypatch.setattr(service_cmd, "_provider_configured", lambda: False)
    monkeypatch.setattr(service_cmd, "_ensure_linger_linux", lambda: None)
    monkeypatch.setattr(service_cmd, "_run_cmd", lambda args, check=True: _result())

    with pytest.raises(typer.Exit) as exc:
        _install()

    assert exc.value.exit_code == 1
    assert unit_path.exists()
    assert "not starting the service" in output.getvalue()


def test_nuitka_binary_remains_the_service_executable(tmp_path, monkeypatch):
    """Desktop's bundled executable must keep winning over PATH launchers."""
    bundled = tmp_path / "Flowly.app" / "Contents" / "Resources" / "flowly-bin"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(bundled))
    monkeypatch.setattr(sys, "argv", ["flowly"])
    monkeypatch.setattr(service_cmd.shutil, "which", lambda _name: "/usr/local/bin/flowly")

    assert service_cmd._resolve_flowly_exec_argv() == [str(bundled.resolve())]


def test_default_startup_wait_fits_desktop_cli_timeout():
    """Desktop terminates ordinary embedded CLI calls after 30 seconds."""
    worst_case_default_windows_wait = (
        service_cmd.WINDOWS_TASK_CREATE_TIMEOUT
        + service_cmd.SERVICE_HEALTH_TIMEOUT
        + service_cmd.SERVICE_HEALTH_REQUEST_TIMEOUT
        + service_cmd.SERVICE_HEALTH_POLL_INTERVAL
    )
    assert service_cmd.SERVICE_HEALTH_TIMEOUT == 20.0
    assert worst_case_default_windows_wait < 30.0
