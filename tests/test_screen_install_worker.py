"""Execute the actual stdlib worker logic with package subprocesses replaced."""

import json
import os
import subprocess
import types
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime.install_payload import WORKER

fcntl = pytest.importorskip("fcntl")

OPERATION, PLAN = "1" * 32, "a" * 64


@pytest.fixture
def worker(tmp_path, monkeypatch):
    worker = types.ModuleType("screen_install_worker_test")
    source = WORKER.rsplit("\ntry:\n    sys.exit(main())", 1)[0]
    exec(compile(source, "<screen-install-worker>", "exec"), worker.__dict__)
    worker.ROOT = tmp_path / "receipts"
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setattr(worker.sys, "argv", ["worker", OPERATION, PLAN, "aarch64"])
    monkeypatch.setattr(worker.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(worker.platform, "freedesktop_os_release", lambda: {"ID": "debian", "VERSION_ID": "12"})
    # Test files are owned by the developer; emulate only their kernel UID.
    fstat, lstat = os.fstat, worker.Path.lstat
    def root_info(info):
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)
    monkeypatch.setattr(worker.os, "fstat", lambda fd: root_info(fstat(fd)))
    monkeypatch.setattr(worker.Path, "lstat", lambda path: root_info(lstat(path)))
    worker.provision_runtime_account = Mock()
    worker.command = Mock(return_value=(0, b""))
    return worker


def test_success_is_durable_idempotent_and_has_fixed_argv(worker, capsys):
    assert worker.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "succeeded" and result["phase"] == "complete"
    assert len(worker.command.call_args_list) == 3
    audit, update, install = worker.command.call_args_list
    assert audit.args == (["/usr/bin/dpkg", "--audit"], 30)
    assert update.args[0][-1] == "update" and update.args[1] == 180
    assert install.args[0][-len(worker.PACKAGES):] == list(worker.PACKAGES)
    assert "--no-remove" in install.args[0] and install.args[1] == 600
    assert worker.main() == 0
    assert json.loads(capsys.readouterr().out) == result
    assert worker.command.call_count == 3
    assert (worker.ROOT / ("1000-" + OPERATION + ".json")).stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("responses,code,state,calls", [
    # Unfinished package work this installer did not leave is not touched: nothing changed.
    ([(0, b"dirty")], "INSTALL_HOST_PACKAGES_BROKEN", "failed", 1),
    ([(0, b""), (100, b"")], "INSTALL_INDEX_FAILED", "failed", 2),
    ([(0, b""), (0, b""), (100, b"")], "INSTALL_PACKAGES_FAILED", "recovery_required", 3),
    ([TimeoutError()], "INSTALL_TIMEOUT", "recovery_required", 1),
    ([RuntimeError("private diagnostic")], "INSTALL_RECOVERY_REQUIRED", "recovery_required", 1),
])
def test_failures_are_typed_durable_and_never_replayed(worker, capsys, responses, code, state, calls):
    worker.command.side_effect = responses
    assert worker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == code and result["state"] == state
    assert "private" not in str(result)
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == result
    assert worker.command.call_count == calls


def test_lock_is_host_wide_and_does_not_create_an_operation_on_contention(worker, capsys):
    worker.directory()
    with (worker.ROOT / "packages.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worker.main() == 1
        assert json.loads(capsys.readouterr().out) == {"error": "INSTALL_BUSY"}
    assert not list(worker.ROOT.glob("*.json"))
    worker.command.assert_not_called()


REPAIR = [(["/usr/bin/dpkg", "--configure", "-a"], 300)]


def receipt(state, updated, operation=OPERATION, uid="1000", error=None):
    return {"installVersion": 1, "operationId": operation, "planId": PLAN, "state": state,
            "phase": "complete" if state == "succeeded" else "installing",
            "error": error if error or state in {"succeeded", "running"} else "INSTALL_RECOVERY_REQUIRED",
            "updatedAt": updated}


def test_an_interrupted_operation_is_reported_never_replayed_and_repaired_by_a_new_one(worker, capsys, monkeypatch):
    worker.directory()
    worker.save(worker.ROOT / ("1000-" + OPERATION + ".json"), receipt("running", 1))
    # The interrupted operation itself only reports what happened.
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out)["state"] == "recovery_required"
    worker.command.assert_not_called()
    # A new operation, from any account, repairs before running the same recipe.
    monkeypatch.setenv("SUDO_UID", "1001")
    monkeypatch.setattr(worker.sys, "argv", ["worker", "2" * 32, PLAN, "aarch64"])
    assert worker.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"
    calls = [call.args for call in worker.command.call_args_list]
    assert calls[0] == REPAIR[0]
    repair = calls[1][0]
    assert repair[-5:] == ["install", "-f", "-y", "--no-install-recommends", "--no-remove"] and calls[1][1] == 600
    assert calls[2] == (["/usr/bin/dpkg", "--audit"], 30)
    assert calls[-1][0][-len(worker.PACKAGES):] == list(worker.PACKAGES)
    # History stays as it happened.
    assert worker.read(worker.ROOT / ("1000-" + OPERATION + ".json"))["state"] == "recovery_required"


def test_a_later_success_means_nothing_is_left_to_repair(worker, capsys):
    worker.directory()
    worker.save(worker.ROOT / ("1000-" + "3" * 32 + ".json"), receipt("recovery_required", 1, "3" * 32))
    worker.save(worker.ROOT / ("1000-" + "4" * 32 + ".json"), receipt("succeeded", 2, "4" * 32))
    assert worker.main() == 0
    assert worker.command.call_args_list[0].args == (["/usr/bin/dpkg", "--audit"], 30)
    assert len(worker.command.call_args_list) == 3


@pytest.mark.parametrize("responses", [[(1, b"")], [(0, b""), (100, b"")], [(0, b""), (0, b""), (0, b"dirty")]])
def test_a_repair_that_does_not_converge_needs_recovery(worker, capsys, responses):
    worker.directory()
    worker.save(worker.ROOT / ("1000-" + "3" * 32 + ".json"), receipt("recovery_required", 1, "3" * 32))
    worker.command.side_effect = responses
    assert worker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "recovery_required" and result["error"] == "INSTALL_PACKAGES_FAILED"


def test_symlink_lock_and_corrupted_receipt_fail_closed(worker):
    worker.directory()
    victim = worker.ROOT / "victim"
    victim.write_text("do not overwrite")
    (worker.ROOT / "packages.lock").symlink_to(victim)
    with pytest.raises(OSError):
        worker.main()
    assert victim.read_text() == "do not overwrite"
    (worker.ROOT / "packages.lock").unlink()
    (worker.ROOT / ("1000-" + OPERATION + ".json")).write_text('{"state":"succeeded"}')
    with pytest.raises(ValueError):
        worker.main()
    worker.command.assert_not_called()


def test_changed_plan_cannot_reuse_operation(worker, capsys, monkeypatch):
    assert worker.main() == 0
    capsys.readouterr()
    monkeypatch.setattr(worker.sys, "argv", ["worker", OPERATION, "b" * 64, "aarch64"])
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == {"error": "OPERATION_CONFLICT"}
    assert worker.command.call_count == 3


def test_worker_restores_child_reaping_before_package_commands(worker, monkeypatch):
    configure = Mock()
    monkeypatch.setattr(worker.signal, "signal", configure)
    def command(*args, **kwargs):
        configure.assert_called_once_with(worker.signal.SIGCHLD, worker.signal.SIG_DFL)
        return 0, b""
    worker.command.side_effect = command
    assert worker.main() == 0


def test_command_timeout_signals_only_its_own_process_group(tmp_path, monkeypatch):
    worker = types.ModuleType("screen_install_command_test")
    exec(compile(WORKER.rsplit("\ntry:\n    sys.exit(main())", 1)[0], "<worker>", "exec"), worker.__dict__)
    process = Mock(pid=4242)
    process.wait.side_effect = [subprocess.TimeoutExpired("apt", 1), 0]
    spawn, signal = Mock(return_value=process), Mock()
    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    monkeypatch.setattr(worker.os, "killpg", signal)
    waitid = Mock(return_value=object())
    monkeypatch.setattr(worker.os, "waitid", waitid, raising=False)
    monkeypatch.setattr(worker.os, "WNOWAIT", 0x1000000, raising=False)
    with pytest.raises(TimeoutError):
        worker.command(["/usr/bin/apt-get", "update"], 1)
    assert spawn.call_args.kwargs["start_new_session"]
    assert spawn.call_args.kwargs["env"] == worker.ENV
    assert all(call.args[0] == 4242 for call in signal.call_args_list)
    assert [call.args[1] for call in signal.call_args_list] == [worker.signal.SIGTERM, worker.signal.SIGKILL]
    assert waitid.call_args.args[1] == 4242
    assert waitid.call_args.args[2] & worker.os.WNOWAIT


def test_root_worker_provisions_account_before_success_and_never_replays(worker, monkeypatch, capsys):
    monkeypatch.delenv("SUDO_UID")
    def provision():
        assert worker.command.call_count == 3
        receipt = json.loads((worker.ROOT / ("0-" + OPERATION + ".json")).read_text())
        assert receipt["state"] == "running" and receipt["phase"] == "installing"
    worker.provision_runtime_account.side_effect = provision
    assert worker.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"
    assert worker.main() == 0
    worker.provision_runtime_account.assert_called_once()


def test_account_failure_is_durable_and_blocks_python_success(worker, monkeypatch, capsys):
    monkeypatch.delenv("SUDO_UID")
    worker.provision_runtime_account.side_effect = ValueError("private detail")
    assert worker.main() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "recovery_required"
    assert receipt["error"] == "INSTALL_RECOVERY_REQUIRED"
    assert "private" not in str(receipt)
    assert worker.main() == 1
    worker.provision_runtime_account.assert_called_once()


def test_nonroot_caller_does_not_provision_an_account(worker):
    assert worker.main() == 0
    worker.provision_runtime_account.assert_not_called()


def test_someone_elses_account_is_a_plain_failure_with_its_own_reason(worker, capsys, monkeypatch):
    monkeypatch.setenv("SUDO_UID", "0")
    worker.provision_runtime_account = Mock(side_effect=ValueError("INSTALL_ACCOUNT_CONFLICT"))
    assert worker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "failed" and result["error"] == "INSTALL_ACCOUNT_CONFLICT"


def test_an_interrupted_account_creation_still_needs_repair(worker, capsys, monkeypatch):
    monkeypatch.setenv("SUDO_UID", "0")
    worker.provision_runtime_account = Mock(side_effect=ValueError("INSTALL_RECOVERY_REQUIRED"))
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out)["state"] == "recovery_required"
