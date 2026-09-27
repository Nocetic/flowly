"""Optional dependencies are pinned, virtualenv-scoped and never elevated."""

import io
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import python_install_worker as worker
from flowly.desktop_runtime import python_installer as installer
from flowly.desktop_runtime.authority import ScreenError

OPERATION = "1" * 32


def test_wheel_hashes_and_full_dependency_closure_match_lockfile():
    root = Path(__file__).parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    closure, pending = set(), ["playwright"]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending.extend(d["name"] for d in packages[name].get("dependencies", []))
    assert closure == set(worker.VERSIONS)
    _, requirements, _ = installer._recipe()
    lines = requirements.decode().replace("\\\n", " ").splitlines()
    actual = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        requirement, *hashes = line.split()
        name, version = requirement.split("==")
        actual[name] = (version, {h.removeprefix("--hash=") for h in hashes})
        assert all(re.fullmatch(r"--hash=sha256:[a-f0-9]{64}", h) for h in hashes)
    assert set(actual) == closure
    for name in closure:
        assert actual[name] == (packages[name]["version"], {
            w["hash"] for w in packages[name]["wheels"] if "manylinux" in w["url"] or "none-any.whl" in w["url"]})
        assert worker.VERSIONS[name] == packages[name]["version"]


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(installer.sys, "base_prefix", str(tmp_path / "base"))
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv/pyvenv.cfg").write_text("include-system-site-packages = false\n")
    monkeypatch.setattr(installer.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/local/bin/uv")
    monkeypatch.setattr(worker, "ready", lambda: False)
    plan = installer.python_plan()
    receipt = {"installVersion": 1, "operationId": OPERATION, "planId": plan["planId"], "state": "succeeded",
               "phase": "complete", "error": None, "updatedAt": 1, "restartRequired": True}
    child = SimpleNamespace(stdout=io.BytesIO(), poll=Mock(return_value=0), communicate=Mock(return_value=(json.dumps(receipt).encode(), None)))
    spawn = Mock(return_value=child)
    monkeypatch.setattr(installer.subprocess, "Popen", spawn)
    return SimpleNamespace(plan=plan, receipt=receipt, child=child, spawn=spawn, home=tmp_path)


def test_plan_does_not_create_state_and_stays_stable_after_packages_are_installed(host, monkeypatch):
    assert host.plan["supported"] and not host.plan["requiresRoot"]
    assert not host.plan["upToDate"]
    assert not worker.state_root().exists()
    monkeypatch.setattr(worker, "ready", lambda: True)
    assert installer.python_plan()["planId"] == host.plan["planId"]
    assert installer.python_plan()["upToDate"]
    host.spawn.assert_not_called()


@pytest.mark.parametrize("kind,code", [("system", "INSTALL_VIRTUALENV_REQUIRED"), ("windows", "INSTALL_UNSUPPORTED_HOST"),
    ("uv", "INSTALL_UV_REQUIRED"), ("frozen", "INSTALL_MANAGED_BUILD_REQUIRED")])
def test_unsupported_installation_never_spawns(host, monkeypatch, kind, code):
    if kind == "system":
        monkeypatch.setattr(installer.sys, "prefix", sys.base_prefix)
    elif kind == "windows":
        monkeypatch.setattr(installer.sys, "platform", "win32")
    elif kind == "uv":
        monkeypatch.setattr(installer.shutil, "which", lambda name: None)
        monkeypatch.setattr(installer.Path, "home", lambda: host.home / "empty-home")
    else:
        monkeypatch.setattr(installer.sys, "frozen", True, raising=False)
    assert installer.python_plan()["reason"] == code
    with pytest.raises(ScreenError, match=code):
        installer.install_python(host.plan["planId"], OPERATION)
    host.spawn.assert_not_called()


@pytest.mark.parametrize("location", [".local/bin", ".cargo/bin"])
def test_service_path_without_uv_uses_the_installer_location(host, monkeypatch, location):
    # A systemd user unit's PATH omits ~/.local/bin, where install.sh puts uv.
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer.Path, "home", lambda: host.home / "user")
    binary = host.home / "user" / location / "uv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    plan = installer.python_plan()
    assert plan["supported"], plan["reason"]
    assert plan["planId"] != host.plan["planId"], "the executed uv is part of the reviewed plan"
    host.child.communicate.return_value = (json.dumps({**host.receipt, "planId": plan["planId"]}).encode(), None)
    installer.install_python(plan["planId"], OPERATION)
    assert host.spawn.call_args.args[0][5] == str(binary.resolve())


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o644])
def test_untrusted_fallback_uv_is_never_executed(host, monkeypatch, mode):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer.Path, "home", lambda: host.home / "user")
    binary = host.home / "user" / ".local" / "bin" / "uv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(mode)
    assert installer.python_plan()["reason"] == "INSTALL_UV_REQUIRED"
    with pytest.raises(ScreenError, match="INSTALL_UV_REQUIRED"):
        installer.install_python(host.plan["planId"], OPERATION)
    host.spawn.assert_not_called()


def test_plan_is_bound_to_current_environment(host, monkeypatch):
    monkeypatch.setattr(installer.sys, "executable", "/other/venv/bin/python")
    with pytest.raises(ScreenError, match="INSTALL_PLAN_CHANGED"):
        installer.install_python(host.plan["planId"], OPERATION)
    host.spawn.assert_not_called()


def test_launch_never_uses_sudo_or_inherits_credentials(host, monkeypatch):
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://credential@example.invalid")
    assert installer.install_python(host.plan["planId"], OPERATION) == host.receipt
    command = host.spawn.call_args.args[0]
    assert command[:2] == [sys.executable, "-I"]
    assert "sudo" not in str(command)
    assert "credential" not in str(host.spawn.call_args)
    assert host.spawn.call_args.kwargs["start_new_session"]
    assert host.child.stdout.closed


def test_observation_timeout_does_not_wait_unbounded_or_replay(host):
    host.child.communicate.side_effect = subprocess.TimeoutExpired("private", 330)
    with pytest.raises(ScreenError, match="INSTALL_TIMEOUT"):
        installer.install_python(host.plan["planId"], OPERATION)
    host.spawn.assert_called_once()
    host.child.poll.assert_called_once()
    assert host.child.stdout.closed


@pytest.mark.parametrize("raw", [b"private output", b"x" * 5000, b'{"error":["private"]}', b'{}'])
def test_malformed_worker_responses_never_escape(host, raw):
    host.child.communicate.return_value = (raw, None)
    with pytest.raises(ScreenError, match="INSTALL_STATE_INVALID"):
        installer.install_python(host.plan["planId"], OPERATION)


@pytest.fixture
def running_worker(tmp_path, monkeypatch):
    pytest.importorskip("fcntl")
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(worker.sys, "base_prefix", str(tmp_path / "base"))
    (tmp_path / "venv").mkdir()
    _, _, digest = installer._recipe()
    monkeypatch.setattr(worker.sys, "argv", ["worker", OPERATION, "a" * 64, "/usr/local/bin/uv", digest])
    install = Mock(return_value=0)
    monkeypatch.setattr(worker, "install", install)
    monkeypatch.setattr(worker, "ready", Mock(side_effect=[False, True]))
    monkeypatch.setattr(worker, "verify", lambda: True)
    return SimpleNamespace(install=install, root=worker.state_root())


def test_worker_persists_results_and_retries_without_installing(running_worker, capsys):
    assert worker.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["restartRequired"] and result["state"] == "succeeded"
    assert worker.main() == 0
    assert json.loads(capsys.readouterr().out) == result
    running_worker.install.assert_called_once()
    assert (running_worker.root / (OPERATION + ".json")).stat().st_mode & 0o777 == 0o600


def test_matching_versions_do_not_touch_environment_or_require_restart(running_worker, monkeypatch, capsys):
    monkeypatch.setattr(worker, "ready", lambda: True)
    assert worker.main() == 0
    assert not json.loads(capsys.readouterr().out)["restartRequired"]
    running_worker.install.assert_not_called()


def test_metadata_alone_does_not_certify_a_working_driver(running_worker, monkeypatch, capsys):
    monkeypatch.setattr(worker, "verify", lambda: False)
    assert worker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "recovery_required" and result["error"] == "INSTALL_VERIFICATION_FAILED"


def test_changed_requirements_fail_before_creating_state(running_worker, monkeypatch, capsys):
    monkeypatch.setattr(worker.sys, "argv", ["worker", OPERATION, "a" * 64, "/usr/local/bin/uv", "0" * 64])
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == {"error": "INSTALL_PLAN_CHANGED"}
    assert not running_worker.root.exists()
    running_worker.install.assert_not_called()


@pytest.mark.parametrize("failure,code", [(1, "INSTALL_PYTHON_FAILED"), (TimeoutError(), "INSTALL_TIMEOUT"),
    (RuntimeError("private"), "INSTALL_RECOVERY_REQUIRED")])
def test_partial_install_is_never_automatically_retried(running_worker, capsys, failure, code):
    if isinstance(failure, Exception):
        running_worker.install.side_effect = failure
    else:
        running_worker.install.return_value = failure
    assert worker.main() == 1
    value = json.loads(capsys.readouterr().out)
    assert value["state"] == "recovery_required" and value["error"] == code
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == value
    running_worker.install.assert_called_once()


def test_venv_lock_is_shared_across_profiles_and_status_detects_lost_worker(running_worker, capsys):
    import fcntl
    worker.private_directory(running_worker.root, create=True)
    fd = os.open(running_worker.root / "python.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worker.main() == 1
        assert json.loads(capsys.readouterr().out) == {"error": "INSTALL_BUSY"}
        value = {"installVersion": 1, "operationId": OPERATION, "planId": "a" * 64, "state": "running",
                 "phase": "installing", "error": None, "updatedAt": 1, "restartRequired": True}
        worker.save(running_worker.root / (OPERATION + ".json"), value)
        assert installer.python_status(OPERATION) == value
    finally:
        os.close(fd)
    assert installer.python_status(OPERATION)["state"] == "recovery_required"
    running_worker.install.assert_not_called()


def test_uv_command_enforces_hashes_and_no_implicit_dependency_or_interpreter_download(monkeypatch, tmp_path):
    process = Mock()
    process.wait.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    assert worker.install("/usr/local/bin/uv", tmp_path / "requirements.txt") == 0
    command = spawn.call_args.args[0]
    for flag in ("--no-config", "--no-cache", "--no-python-downloads", "--require-hashes", "--only-binary", "--no-deps"):
        assert flag in command
    assert command[command.index("--python") + 1] == sys.executable
    assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
    assert spawn.call_args.kwargs["stderr"] == subprocess.DEVNULL
