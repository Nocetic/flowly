"""A screen cannot silently run outside its requested resource boundary."""

from pathlib import Path

import pytest

from flowly.desktop_runtime import resource_scope as scope
from flowly.desktop_runtime.authority import ScreenError

GENERATION = "a" * 32


def test_scope_keeps_the_gateway_out_and_profiles_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "available", lambda: True)
    monkeypatch.setattr(scope.shutil, "which", lambda *a, **k: "/usr/bin/systemd-run")
    monkeypatch.setattr(scope.os, "geteuid", lambda: 1000)
    command, enabled = scope.scoped_command(["python", "worker"], tmp_path, GENERATION)
    assert enabled and "--user" in command and "--scope" in command
    assert command[-3:] == ["--", "python", "worker"]
    assert f"--property=MemoryMax={scope.MEMORY_MAX}" in command
    assert scope.scope_name(tmp_path, GENERATION) != scope.scope_name(tmp_path / "other", GENERATION)
    assert scope.scope_name(tmp_path, GENERATION) != scope.scope_name(tmp_path, "b" * 32)


def test_missing_manager_is_explicit_but_failed_admission_cannot_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "available", lambda: False)
    assert scope.scoped_command(["worker"], tmp_path, GENERATION) == (["worker"], False)
    monkeypatch.setattr(scope, "available", lambda: True)
    monkeypatch.setattr(scope.shutil, "which", lambda *a, **k: None)
    with pytest.raises(ScreenError, match="RUNTIME_START_FAILED"):
        scope.scoped_command(["worker"], tmp_path, GENERATION)


@pytest.fixture
def kernel(tmp_path):
    proc, root = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    unit = scope.scope_name(tmp_path, GENERATION)
    path = root / "system.slice" / unit
    path.mkdir(parents=True)
    (proc / "self/cgroup").write_text(f"0::/system.slice/{unit}\n")
    for name, value in {"memory.max": scope.MEMORY_MAX, "memory.high": scope.MEMORY_HIGH,
                        "memory.swap.max": scope.SWAP_MAX, "pids.max": scope.TASKS_MAX}.items():
        (path / name).write_text(str(value))
    return tmp_path, proc, root, path


def test_limits_are_verified_from_kernel(kernel):
    directory, proc, root, _ = kernel
    assert scope.verify_scope(directory, GENERATION, proc=proc, root=root)["enforced"]


@pytest.mark.parametrize("counter,value", [("memory.max", "max"), ("memory.max", str(2 * scope.MEMORY_MAX)),
                                         ("memory.swap.max", "max"), ("pids.max", "max")])
def test_unenforced_limit_is_not_advertised(kernel, counter, value):
    directory, proc, root, path = kernel
    (path / counter).write_text(value)
    with pytest.raises(ScreenError, match="RUNTIME_START_FAILED"):
        scope.verify_scope(directory, GENERATION, proc=proc, root=root)


def test_gateway_or_other_generation_is_not_accepted(kernel):
    directory, proc, root, _ = kernel
    (proc / "self/cgroup").write_text("0::/system.slice/ai.flowly.gateway.service\n")
    with pytest.raises(ScreenError, match="RUNTIME_START_FAILED"):
        scope.verify_scope(directory, GENERATION, proc=proc, root=root)


@pytest.mark.parametrize("generation", ["", "../gateway", "x" * 32, "a" * 33])
def test_persisted_scope_cannot_name_another_service(generation):
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        scope.scope_name(Path("/tmp/profile"), generation)
