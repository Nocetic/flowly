"""Restart barriers use kernel process age, not PIDs or wall-clock timestamps."""

import os
import subprocess
import sys
import time

import pytest

from flowly.desktop_runtime import install_jobs as jobs
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.handoff import process_identity
from tests.test_screen_install_jobs import OP, PLAN, finished
from tests.test_screen_install_jobs import fixture as job_fixture

fixture = job_fixture


def test_completion_clock_uses_boot_ticks(monkeypatch):
    monkeypatch.setattr(jobs.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(jobs.time, "clock_gettime_ns", lambda clock: 123_456_789_123 if clock == 7 else None)
    monkeypatch.setattr(jobs.os, "sysconf", lambda key: 100 if key == "SC_CLK_TCK" else None)
    monkeypatch.setattr(jobs.time, "time", lambda: -999999)
    assert jobs._restart_boundary({"boot": "boot"}) == {"boot": "boot", "ticks": 12345}


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux /proc process start clock")
@pytest.mark.parametrize("runtime", [False, pytest.param(True, marks=pytest.mark.skipif(
    os.environ.get("FLOWLY_SCREEN_LINUX_TEST") != "1", reason="requires disposable Xvnc desktop image"))])
def test_real_old_process_is_blocked_and_fresh_interpreter_is_allowed(fixture, monkeypatch, runtime):
    service, _ = fixture
    # Restore real process identity/clock; package execution remains mocked.
    monkeypatch.setattr(jobs, "process_identity", process_identity)
    monkeypatch.setattr(jobs, "_restart_boundary", REAL_BOUNDARY)
    service.submit(PLAN, OP)
    assert finished(service)["state"] == "succeeded"
    with pytest.raises(ScreenError, match="INSTALL_RESTART_REQUIRED"):
        jobs.assert_environment_installation_idle()
    arguments = []
    if runtime:
        from flowly.desktop_runtime.launcher import ScreenRuntime
        desktop = service.root.parent / "desktop"
        deadline = time.monotonic() + 3
        while True:
            try:
                ScreenRuntime(desktop).start()
            except ScreenError as exc:
                if exc.code == "INSTALL_BUSY" and time.monotonic() < deadline:
                    time.sleep(.005)
                    continue
                assert exc.code == "INSTALL_RESTART_REQUIRED"
                break
            pytest.fail("old interpreter started a desktop after package changes")
        arguments = [str(desktop)]
    # A new process within the completion tick is intentionally conservative.
    time.sleep(2 / os.sysconf("SC_CLK_TCK"))
    subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from flowly.desktop_runtime import install_jobs
install_jobs.state_root = lambda: Path(sys.argv[1])
install_jobs.assert_environment_installation_idle()
if len(sys.argv) > 2:
    from flowly.desktop_runtime.launcher import ScreenRuntime
    runtime = ScreenRuntime(Path(sys.argv[2]))
    state = runtime.start()
    try:
        assert state['phase'] == 'running'
    finally:
        runtime.stop(state['generation'])
""", str(service.root), *arguments], check=True, timeout=60)


REAL_BOUNDARY = jobs._restart_boundary
