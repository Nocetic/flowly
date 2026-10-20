"""Real hash-checked wheels into a fresh, unprivileged disposable virtualenv."""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import venv

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_PYTHON_INSTALL_TEST") != "1",
                                reason="requires disposable Linux image with uv and network")


def run(python, script, *args, timeout=350):
    result = subprocess.run([str(python), "-c", script, *args], text=True, capture_output=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def test_real_python_install_survives_caller_exit_and_preserves_other_environments(tmp_path):
    assert os.geteuid() != 0
    uv = shutil.which("uv")
    assert uv
    target = tmp_path / "agent-venv"
    venv.EnvBuilder(with_pip=False).create(target)
    python = target / "bin/python"
    # Only Core's lightweight lock dependency is needed to run this API in a
    # bare venv. Full agent dependencies remain in the image's other environment.
    subprocess.run([uv, "--no-config", "pip", "install", "--python", str(python), "filelock==3.19.1"],
                   check=True, capture_output=True, timeout=90)
    inventory = "import json,importlib.metadata as m; print(json.dumps(sorted((d.metadata['Name'],d.version) for d in m.distributions())))"
    snapshot = run(sys.executable, inventory)
    prefix = "from flowly.desktop_runtime.python_installer import python_plan, install_python, python_status; import json; "
    plan = run(python, prefix + "print(json.dumps(python_plan()))")
    assert plan["supported"] and not plan["upToDate"] and not plan["requiresRoot"]
    assert not (target / ".flowly-screen-install").exists()
    operation = secrets.token_hex(16)
    script = prefix + "import sys; print(json.dumps(install_python(sys.argv[1],sys.argv[2])))"
    caller = subprocess.Popen([str(python), "-c", script, plan["planId"], operation],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = target / ".flowly-screen-install" / (operation + ".json")
    try:
        deadline = time.monotonic() + 20
        while not path.exists():
            assert time.monotonic() < deadline and caller.poll() is None
            time.sleep(0.05)
        assert json.loads(path.read_text())["state"] == "running"
        caller.kill()
        caller.wait(timeout=5)
        deadline = time.monotonic() + 330
        while json.loads(path.read_text())["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5)
    receipt = run(python, prefix + "import sys; print(json.dumps(python_status(sys.argv[1])))", operation)
    assert receipt["state"] == "succeeded", receipt
    assert receipt["restartRequired"]
    assert run(python, prefix + "print(json.dumps(python_plan()))")["planId"] == plan["planId"]
    assert run(python, prefix + "print(json.dumps(python_plan()))")["upToDate"]
    assert run(python, script, plan["planId"], operation) == receipt
    unchanged = run(python, script, plan["planId"], secrets.token_hex(16))
    assert unchanged["state"] == "succeeded" and not unchanged["restartRequired"]
    # Actually import and start the installed Playwright driver (not a browser
    # download), verifying the optional package is usable in that exact venv.
    assert run(python, "from playwright.sync_api import sync_playwright; import json; "
               "p=sync_playwright().start(); print(json.dumps(p.chromium.name)); p.stop()") == "chromium"
    assert run(sys.executable, inventory) == snapshot


def test_real_uv_rejects_a_wrong_wheel_hash(tmp_path):
    from flowly.desktop_runtime import python_install_worker
    uv = shutil.which("uv")
    assert uv
    requirements = tmp_path / "bad-hash.txt"
    # Use a bare environment so an already installed package cannot bypass the
    # download. The invalid hash must be rejected before any package is written.
    target = tmp_path / "hash-test-venv"
    venv.EnvBuilder(with_pip=False).create(target)
    python = target / "bin/python"
    requirements.write_text("filelock==3.19.1 --hash=sha256:" + "0" * 64 + "\n")
    script = """
import sys, json, importlib.util, subprocess
s = importlib.util.spec_from_file_location('worker', sys.argv[1])
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
original = subprocess.Popen
children = []
def spawn(*args, **kwargs):
    kwargs['stderr'] = subprocess.PIPE
    child = original(*args, **kwargs)
    children.append(child)
    return child
m.subprocess.Popen = spawn
code = m.install(sys.argv[2], sys.argv[3])
error = children[0].stderr.read(16384)
children[0].stderr.close()
print(json.dumps({'code': code, 'hashRejected': b'Hash mismatch' in error}))
"""
    result = run(python, script, python_install_worker.__file__, uv, str(requirements))
    assert result["code"] != 0 and result["hashRejected"], result
    assert run(python, "import json,importlib.util; print(json.dumps(importlib.util.find_spec('filelock') is None))")
