"""Service hardening: keep the gateway up on Linux and Windows.

Linux — the systemd user unit must disable the start-rate limiter so a quick
early crash-loop can't push the unit into a permanent ``failed`` state where
``Restart=always`` no longer helps.

Windows — the Task Scheduler launcher must be a *console-less supervisor loop*,
not fire-and-forget. The old launcher ran ``wscript -> cmd /c flowly … `` and
returned instantly, so Task Scheduler marked the task "finished OK", stopped
watching, and a mid-life gateway crash stayed down for hours (the reported bug).
The new launcher loops: it relaunches the gateway whenever it exits, runs with
no cmd.exe console (so a logon CTRL_CLOSE can't reap it as a "user cancel"), and
honours a stop-flag so ``flowly service stop`` ends it cleanly.

No real ``schtasks``/``systemctl`` is invoked — we monkeypatch the OS calls and
assert on the generated artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── Linux ────────────────────────────────────────────────────────────────────

def test_linux_unit_disables_start_limit_and_restarts_always():
    from flowly.cli.service_cmd import _build_linux_unit

    unit = _build_linux_unit(
        exec_line="/opt/flowly/flowly gateway --port 18790",
        flowly_home="/home/u/.flowly",
        runtime_cwd="",
    )
    # Restart-on-crash stays on, and the rate limiter is disabled so a fast
    # early crash-loop never lands in a permanent `failed` state.
    assert "Restart=always" in unit
    assert "StartLimitIntervalSec=0" in unit


# ── Windows ──────────────────────────────────────────────────────────────────

@pytest.fixture
def _win_install(tmp_path, monkeypatch):
    """Run ``service_install`` as if on Windows and return (vbs_text, xml_text)."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    from flowly.cli import service_cmd

    win_xml = tmp_path / "flowly" / "ai.flowly.gateway.xml"
    monkeypatch.setattr(service_cmd, "_service_paths", lambda label: (None, None, win_xml))
    monkeypatch.setattr(service_cmd, "_get_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(service_cmd, "_is_windows_admin", lambda: True)
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        service_cmd, "_resolve_flowly_exec_argv", lambda: [r"C:\Program Files\flowly\flowly.exe"]
    )

    def fake_run(args, **kw):  # stand in for schtasks /create
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(service_cmd.subprocess, "run", fake_run)

    # Call the Typer command function directly — every parameter must be passed
    # explicitly, otherwise the typer.Option(...) sentinels leak in as values.
    service_cmd.service_install(
        label="ai.flowly.gateway", port=18790, verbose=False, start=False,
        force=False, persona="", cwd="", host="", remote=False, token="",
    )

    vbs = (win_xml.parent / "ai.flowly.gateway.vbs").read_text(encoding="utf-8")
    xml = win_xml.read_text(encoding="utf-16")
    return vbs, xml


def test_windows_launcher_is_a_supervisor_loop(_win_install):
    vbs, _xml = _win_install
    # A real loop, not a one-shot.
    assert "Do" in vbs and "Loop" in vbs
    # Hidden window (0) AND bWaitOnReturn=True → wscript stays alive and is
    # monitored; the old fire-and-forget used ", 0, False".
    assert ", 0, True" in vbs
    assert ", 0, False" not in vbs


def test_windows_launcher_has_no_console(_win_install):
    vbs, _xml = _win_install
    # No cmd.exe anywhere → no console to be reaped by a logon CTRL_CLOSE.
    assert "cmd /c" not in vbs
    # It launches the gateway binary directly.
    assert "flowly.exe" in vbs


def test_resolve_exec_argv_avoids_cmd_shim(monkeypatch):
    """The git-checkout install's Windows launcher (``flowly.cmd``) is on
    PATH — ``shutil.which("flowly")`` resolving to it must NOT be used
    verbatim for the service argv, since a ``.cmd`` requires ``cmd.exe`` to
    execute, reintroducing the console that test_windows_launcher_has_no_console
    exists to forbid. Must fall back to the running interpreter with
    ``-m flowly`` instead — that interpreter IS the venv python (this process
    was itself launched by ``flowly.cmd``'s ``python -m flowly``).
    """
    import sys

    from flowly.cli import service_cmd

    monkeypatch.setattr(sys, "argv", ["__main__.py"])
    monkeypatch.setattr(
        sys, "executable", r"C:\Users\u\AppData\Local\Flowly\venv\Scripts\python.exe"
    )
    monkeypatch.setattr(
        service_cmd.shutil, "which",
        lambda name: r"C:\Users\u\AppData\Local\Flowly\bin\flowly.cmd" if name == "flowly" else None,
    )

    argv = service_cmd._resolve_flowly_exec_argv()

    assert argv[0].lower().endswith("python.exe")
    assert argv[1:] == ["-m", "flowly"]
    assert not any(a.lower().endswith((".cmd", ".bat")) for a in argv)


def test_windows_launcher_honours_stop_flag(_win_install):
    vbs, _xml = _win_install
    # The loop checks a stop-flag so `flowly service stop` can end it cleanly,
    # even on the Startup-folder fallback path.
    assert "stopFlag" in vbs
    assert "FileExists(stopFlag)" in vbs


def test_windows_task_xml_is_hardened(_win_install):
    _vbs, xml = _win_install
    assert "<Command>wscript.exe</Command>" in xml
    # Retry a crash many times, not just 10.
    assert "<Count>999</Count>" in xml
    # Let the desktop settle before first start.
    assert "<Delay>PT30S</Delay>" in xml
    # Never stop when the box goes idle, never time-limit a long-lived service.
    assert "<StopOnIdleEnd>false</StopOnIdleEnd>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
