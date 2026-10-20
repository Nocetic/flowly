"""Actual asynchronous OS + optional Python installation in a disposable host."""

import os
import secrets
import subprocess
import sys
import time

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.install_jobs import ScreenInstallJobs
from flowly.desktop_runtime.install_plan import installation_plan
from flowly.desktop_runtime.installer import install_status
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.python_installer import python_status

pytestmark = pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_INSTALL_JOBS_TEST") != "1",
                                reason="requires the clean install-jobs container")


def test_real_asynchronous_installation_and_durable_replay(tmp_path):
    assert os.geteuid() != 0
    identity = {"hostId": "fixture-host", "botId": "fixture-agent", "profileId": "default", "screenId": "browser"}
    plan = installation_plan(identity)
    assert plan["system"]["supported"] and plan["python"]["supported"]
    assert not plan["python"]["upToDate"]
    assert "Xvnc" in ScreenRuntime.capabilities()["missing"]
    operation = secrets.token_hex(16)
    service = ScreenInstallJobs(identity, lambda: None)
    started = time.monotonic()
    accepted = service.submit(plan["planId"], operation, password="flowly-install-fixture")
    assert time.monotonic() - started < 2, "submission waited for package installation"
    assert accepted["state"] == "pending"
    # New controller objects query the existing job rather than owning its worker.
    replacement = ScreenInstallJobs(identity, lambda: None)
    deadline = time.monotonic() + 1260
    while (result := replacement.status(operation))["state"] in {"pending", "running"}:
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert result["state"] == "succeeded", result
    assert result["completedSteps"] == ["system", "python"] and result["restartRequired"]
    assert install_status(operation)["state"] == "succeeded"
    assert python_status(operation)["state"] == "succeeded"
    assert replacement.submit(plan["planId"], operation, password="never-used-retry-secret") == result
    assert b"flowly-install-fixture" not in service.path.read_bytes()
    assert installation_plan(identity)["planId"] == plan["planId"]
    assert ScreenRuntime.capabilities()["supported"]
    runtime = ScreenRuntime(tmp_path / "desktop")
    assert runtime.read_manifest() is None, "installation must not start a browser"
    with pytest.raises(ScreenError, match="INSTALL_RESTART_REQUIRED"):
        runtime.start()
    time.sleep(2 / os.sysconf("SC_CLK_TCK"))
    subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from flowly.desktop_runtime.launcher import ScreenRuntime
runtime = ScreenRuntime(Path(sys.argv[1]))
state = runtime.start()
try:
    assert state['phase'] == 'running'
finally:
    runtime.stop(state['generation'])
""", str(tmp_path / "desktop")], check=True, timeout=90)
