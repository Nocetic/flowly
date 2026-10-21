"""Fixed package plans and privilege/secret boundaries without host mutation."""

import io
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import installer
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.install_payload import WORKER
from flowly.desktop_runtime.install_recipe import (
    BROWSER_SOURCE,
    RECIPE_SOURCE,
    recipe_id,
    screen_recipe,
)

APT = {"/usr/bin/apt-get", "/usr/bin/dpkg"}
DEBIAN = screen_recipe({"ID": "debian", "VERSION_ID": "12"}, "aarch64", APT.__contains__)

OPERATION = "1" * 32


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer, "os_release", lambda: {"ID": "debian", "VERSION_ID": "12"})
    monkeypatch.setattr(installer.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(installer, "_exists", APT.__contains__)
    monkeypatch.setattr(installer.Path, "is_file", lambda path: True)
    monkeypatch.setattr(installer.os, "access", lambda *args: True)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000, raising=False)
    plan = installer.install_plan()
    receipt = {"installVersion": 1, "operationId": OPERATION, "planId": plan["planId"],
               "state": "succeeded", "phase": "complete", "error": None, "updatedAt": 100}
    child = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), poll=Mock(return_value=0),
                            communicate=Mock(return_value=(json.dumps(receipt).encode(), None)))
    spawn = Mock(return_value=child)
    monkeypatch.setattr(installer.subprocess, "Popen", spawn)
    return SimpleNamespace(plan=plan, receipt=receipt, child=child, spawn=spawn)


def test_plan_is_readonly_and_fingerprints_privileged_source(host, monkeypatch):
    assert host.plan["scope"] == "system-packages"
    assert host.plan["packages"] == DEBIAN["packages"]
    assert host.plan["distribution"] == "debian-12" and host.plan["recipeId"] == recipe_id(DEBIAN)
    assert installer.install_plan() == host.plan
    host.spawn.assert_not_called()
    monkeypatch.setattr(installer, "WORKER", WORKER + "\n# changed")
    assert installer.install_plan()["planId"] != host.plan["planId"]


@pytest.mark.parametrize("distro,version,arch", [("fedora", "42", "x86_64"), ("debian", "11", "aarch64"),
                                                  ("debian", "12", "riscv64"), ("ubuntu", "20.04", "x86_64")])
def test_hosts_without_a_reviewed_recipe_are_not_offered_one(host, monkeypatch, distro, version, arch):
    monkeypatch.setattr(installer, "os_release", lambda: {"ID": distro, "VERSION_ID": version})
    monkeypatch.setattr(installer.platform, "machine", lambda: arch)
    plan = installer.install_plan()
    assert not plan["supported"] and plan["reason"] == "INSTALL_UNSUPPORTED_HOST"
    assert plan["packages"] == [] and plan["browser"] is None and plan["recipeId"] is None


@pytest.mark.parametrize("arch,browser,repositories", [
    ("aarch64", {"source": "pinned"}, []),
    ("x86_64", {"source": "distribution", "package": "google-chrome-stable"}, ["google-chrome"]),
])
def test_ubuntu_is_offered_its_own_recipe_and_browser(host, monkeypatch, arch, browser, repositories):
    monkeypatch.setattr(installer, "os_release", lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"})
    monkeypatch.setattr(installer.platform, "machine", lambda: arch)
    plan = installer.install_plan()
    assert plan["supported"] and plan["distribution"] == "ubuntu-24.04"
    assert browser.items() <= plan["browser"].items() and "chromium" not in plan["packages"]
    # The person reviewing sees the repository Google's package adds.
    assert plan["repositories"] == repositories
    assert plan["planId"] != host.plan["planId"]


def test_plan_change_never_starts_privileged_process(host):
    with pytest.raises(ScreenError, match="INSTALL_PLAN_CHANGED"):
        installer.install_system("0" * 64, OPERATION, password="generated-test-password")
    host.spawn.assert_not_called()


def test_password_only_uses_stdin_and_does_not_enter_command_or_environment(host, monkeypatch):
    monkeypatch.setenv("FLOWLY_API_KEY", "must-not-inherit")
    result = installer.install_system(host.plan["planId"], OPERATION, password="generated-test-password")
    assert result == host.receipt
    args, kwargs = host.spawn.call_args
    assert args[0][:5] == ["/usr/bin/sudo", "-S", "-p", "", "--"]
    assert args[0][5:8] == ["/usr/bin/python3", "-I", "-c"]
    assert "generated-test-password" not in repr(host.spawn.call_args)
    assert "must-not-inherit" not in repr(kwargs)
    assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["start_new_session"]
    host.child.communicate.assert_called_once_with(input=b"generated-test-password\n", timeout=installer.WORKER_SECONDS)
    assert host.child.stdin.closed and host.child.stdout.closed


def test_passwordless_execution_never_prompts(host):
    installer.install_system(host.plan["planId"], OPERATION)
    assert host.spawn.call_args.args[0][:3] == ["/usr/bin/sudo", "-n", "--"]
    host.child.communicate.assert_called_once_with(input=b"", timeout=installer.WORKER_SECONDS)


def test_root_does_not_call_sudo_but_still_requires_runtime_account(host, monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    plan = installer.install_plan()
    assert plan["runtimeAccountRequired"]
    host.receipt["planId"] = plan["planId"]
    host.child.communicate.return_value = (json.dumps(host.receipt).encode(), None)
    installer.install_system(plan["planId"], OPERATION)
    assert host.spawn.call_args.args[0][:3] == ["/usr/bin/python3", "-I", "-c"]


@pytest.mark.parametrize("secret", ["line\nbreak", "line\rbreak", "null\x00byte", "a" * 4097, "ü" * 2049, "\ud800"])
def test_invalid_secret_never_reaches_sudo(host, secret):
    with pytest.raises(ScreenError, match="INVALID_PARAMS"):
        installer.install_system(host.plan["planId"], OPERATION, password=secret)
    host.spawn.assert_not_called()


@pytest.mark.parametrize("operation", ["../outside", "x" * 32, "a" * 33, "a;touch file", None])
def test_operation_is_never_a_path_or_shell_fragment(host, operation):
    with pytest.raises(ScreenError, match="INVALID_PARAMS"):
        installer.install_system(host.plan["planId"], operation)
    host.spawn.assert_not_called()


def test_observation_timeout_never_waits_or_restarts_privileged_worker(host):
    host.child.communicate.side_effect = subprocess.TimeoutExpired("private-command", 850)
    with pytest.raises(ScreenError, match="INSTALL_TIMEOUT"):
        installer.install_system(host.plan["planId"], OPERATION)
    host.spawn.assert_called_once()
    host.child.poll.assert_called_once()
    assert host.child.stdout.closed


@pytest.mark.parametrize("output,code", [(b"sudo private-password denied", "INSTALL_PRIVILEGE_REQUIRED"),
    (b'{"error":"INSTALL_BUSY"}', "INSTALL_BUSY"),
    (b'{"error":"private-password"}', "INSTALL_STATE_INVALID"),
    (b"x" * 5000, "INSTALL_STATE_INVALID")])
def test_private_diagnostics_never_escape(host, output, code):
    host.child.communicate.return_value = (output, None)
    with pytest.raises(ScreenError, match=code):
        installer.install_system(host.plan["planId"], OPERATION)


def test_the_worker_recomputes_exactly_the_reviewed_recipe(host):
    # The privileged worker carries the same recipe source verbatim and is
    # given only the reviewed recipe's id; it installs nothing else.
    assert RECIPE_SOURCE in WORKER and BROWSER_SOURCE in WORKER
    command = None
    installer.install_system(host.plan["planId"], OPERATION)
    command = host.spawn.call_args.args[0]
    assert command[-3:] == [OPERATION, host.plan["planId"], recipe_id(DEBIAN)]
    compile(WORKER, "<screen-install-worker>", "exec")


@pytest.mark.parametrize("updates", [{"state": "succeeded", "error": "INSTALL_TIMEOUT"},
    {"phase": "private-path"}, {"planId": "wrong"}, {"operationId": "other"},
    {"updatedAt": 2**54}, {"extra": "secret"}])
def test_receipt_is_closed_and_consistent(host, updates):
    with pytest.raises(ScreenError, match="INSTALL_STATE_INVALID"):
        installer._receipt({**host.receipt, **updates}, OPERATION, host.plan["planId"])


def test_running_status_requires_an_actual_held_host_lock(tmp_path, monkeypatch):
    import os
    fcntl = pytest.importorskip("fcntl")
    root = tmp_path / "installer"
    root.mkdir()
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer.os, "getuid", lambda: 1000)
    lstat, fstat = installer.Path.lstat, os.fstat
    def root_info(info):
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)
    monkeypatch.setattr(installer.Path, "lstat", lambda path: root_info(lstat(path)))
    monkeypatch.setattr(installer.os, "fstat", lambda fd: root_info(fstat(fd)))
    value = {"installVersion": 1, "operationId": OPERATION, "planId": "a" * 64,
             "state": "running", "phase": "installing", "error": None, "updatedAt": 1}
    path = root / f"1000-{OPERATION}.json"
    path.write_text(json.dumps(value))
    with (root / "packages.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert installer.install_status(OPERATION) == value
    result = installer.install_status(OPERATION)
    assert result["state"] == "recovery_required"
    assert result["error"] == "INSTALL_RECOVERY_REQUIRED"
    # Status reports uncertainty without writing, releasing or replaying jobs.
    assert json.loads(path.read_text()) == value


@pytest.mark.parametrize("updates", [{"state": []}, {"phase": {}}, {"error": []}, {"installVersion": True}])
def test_malformed_receipt_never_raises_a_raw_type_error(host, updates):
    with pytest.raises(ScreenError, match="INSTALL_STATE_INVALID"):
        installer._receipt({**host.receipt, **updates}, OPERATION)
