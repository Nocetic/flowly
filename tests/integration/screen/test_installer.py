"""Real sudo and Debian package installation in a disposable clean container."""

import os
import secrets
import subprocess
import sys
import time
import types

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.install_payload import WORKER
from flowly.desktop_runtime.installer import install_plan, install_status, install_system
from flowly.desktop_runtime.launcher import ScreenRuntime

pytestmark = pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_INSTALL_TEST") != "1",
                                reason="requires clean disposable installer container")


def test_real_privileged_install_and_receipt_replay(tmp_path):
    assert os.geteuid() != 0
    assert "Xvnc" in ScreenRuntime.capabilities()["missing"]
    plan = install_plan()
    assert plan["supported"] and plan["scope"] == "system-packages"
    with pytest.raises(ScreenError, match="INSTALL_PRIVILEGE_REQUIRED"):
        install_system(plan["planId"], secrets.token_hex(16), password="incorrect-generated-password")
    operation = secrets.token_hex(16)
    script = ("import sys; from flowly.desktop_runtime.installer import install_system; "
              "install_system(sys.argv[1], sys.argv[2], password=sys.stdin.readline().rstrip('\\n'))")
    caller = subprocess.Popen([sys.executable, "-c", script, plan["planId"], operation],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        caller.stdin.write(b"flowly-install-fixture\n")
        caller.stdin.close()
        deadline = time.monotonic() + 30
        while True:
            try:
                result = install_status(operation)
                break
            except ScreenError as exc:
                assert exc.code == "OPERATION_NOT_FOUND"
                assert time.monotonic() < deadline and caller.poll() is None
                time.sleep(0.1)
        assert result["state"] == "running"
        # The host lock is shared across profiles and calling processes.
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            install_system(plan["planId"], secrets.token_hex(16), password="flowly-install-fixture")
        caller.kill()
        caller.wait(timeout=5)
        deadline = time.monotonic() + 850
        while (result := install_status(operation))["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.2)
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5)
    assert result["state"] == "succeeded", result
    assert install_status(operation) == result
    assert install_system(plan["planId"], operation, password="flowly-install-fixture") == result
    assert ScreenRuntime.capabilities()["supported"]
    # The package installer never constructs a browser/profile itself.
    runtime = ScreenRuntime(tmp_path / "desktop")
    assert runtime.read_manifest() is None
    state = runtime.start()
    try:
        assert state["phase"] == "running"
    finally:
        runtime.stop(state["generation"])


def test_real_timeout_kills_child_that_survives_group_leader(tmp_path):
    worker = types.ModuleType("screen_install_timeout_test")
    exec(compile(WORKER.rsplit("\ntry:\n    sys.exit(main())", 1)[0], "<worker>", "exec"), worker.__dict__)
    marker = tmp_path / "child-pid"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent = ("import subprocess,sys,time; from pathlib import Path; "
              "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
              "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    with pytest.raises(TimeoutError):
        worker.command([sys.executable, "-c", parent, str(marker), child], 1)
    pid = int(marker.read_text())
    deadline = time.monotonic() + 3
    while True:
        try:
            state = worker.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        assert time.monotonic() < deadline, "package descendant survived timeout"
        time.sleep(0.05)
