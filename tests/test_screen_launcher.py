"""Runtime admission, private state and process identity safety."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.desktop_runtime import launcher
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime, private_directory, write_state


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(
        lambda: dict(supported=True, reason=None, missing=[])))
    return ScreenRuntime(tmp_path / "desktop")


def manifest(runtime, **updates):
    value = dict(schema=1, generation="generation-1", phase="running", supervisor={"pid": 123},
                 children=[{"pid": 124}], error=None)
    value.update(updates)
    write_state(runtime.manifest, value)
    return value


def test_reconnect_adopts_runtime_without_spawning(runtime, monkeypatch):
    expected = manifest(runtime)
    runtime.authority.start("generation-1")
    monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("must adopt"))
    monkeypatch.setattr(launcher, "resource_report", lambda *a: pytest.fail("must not gate running browser"))
    assert runtime.start() == expected
    # Human control survives controller reconnect too.
    runtime.authority.acquire("viewer", "generation-1", 1)
    assert runtime.start() == expected
    assert runtime.authority.snapshot()["control"] == "human"


@pytest.mark.parametrize("reason", ["INSUFFICIENT_RESOURCES", "RESOURCE_CHECK_UNAVAILABLE"])
def test_failed_budget_does_not_reserve_generation_or_spawn(runtime, monkeypatch, reason):
    from filelock import FileLock
    startup = FileLock(runtime.directory / "test-startup.lock")
    monkeypatch.setattr(runtime, "_startup_lock", lambda: startup)
    def report(directory):
        assert runtime._lock.is_locked and startup.is_locked
        assert directory == runtime.directory
        return {"ready": False, "reason": reason}
    monkeypatch.setattr(launcher, "resource_report", report)
    monkeypatch.setattr(runtime, "_spawn", lambda: pytest.fail("insufficient budget"))
    with pytest.raises(ScreenError, match=reason):
        runtime.start()
    assert runtime.read_manifest() is None
    assert not runtime.authority.snapshot()["running"]


def test_new_spawn_holds_both_locks_and_rechecks_identity(runtime, monkeypatch):
    from filelock import FileLock
    startup = FileLock(runtime.directory / "test-startup.lock")
    monkeypatch.setattr(runtime, "_startup_lock", lambda: startup)
    calls = []
    def guard():
        calls.append(startup.is_locked)
    def spawn():
        assert runtime._lock.is_locked and startup.is_locked
        return {"generation": "new"}
    monkeypatch.setattr(launcher, "resource_report", lambda path: {"ready": True})
    monkeypatch.setattr(runtime, "_spawn", spawn)
    assert runtime.start(identity_guard=guard) == {"generation": "new"}
    assert calls == [False, True]


def test_profiles_do_not_concurrently_spend_the_same_startup_budget(runtime, tmp_path, monkeypatch):
    other = ScreenRuntime(tmp_path / "other/desktop")
    account = tmp_path / "account"
    private = launcher.private_directory
    def directory(path):
        return private(account if path == launcher.Path("/tmp") / f"flowly-screen-{os.getuid()}" else path)
    monkeypatch.setattr(launcher, "private_directory", directory)
    started, release, second_waiting = threading.Event(), threading.Event(), threading.Event()
    allocated = False
    def report(path):
        return {"ready": not allocated, "reason": "INSUFFICIENT_RESOURCES" if allocated else None}
    def spawn():
        nonlocal allocated
        started.set()
        assert release.wait(3)
        allocated = True
        return {"generation": "first"}
    second_lock = other._startup_lock
    def lock():
        second_waiting.set()
        return second_lock()
    monkeypatch.setattr(other, "_startup_lock", lock)
    monkeypatch.setattr(runtime, "_spawn", spawn)
    monkeypatch.setattr(other, "_spawn", lambda: pytest.fail("already spent budget"))
    monkeypatch.setattr(launcher, "resource_report", report)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.start)
        try:
            assert started.wait(3)
            second = pool.submit(other.start)
            assert second_waiting.wait(3)
        finally:
            release.set()
        assert first.result(timeout=3) == {"generation": "first"}
        with pytest.raises(ScreenError, match="INSUFFICIENT_RESOURCES"):
            second.result(timeout=3)
    assert other.read_manifest() is None


def test_doctor_does_not_construct_runtime_or_write_profile(tmp_path, monkeypatch):
    directory = tmp_path / "missing/desktop"
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(lambda: {
        "supported": False, "reason": "DEPENDENCIES_MISSING", "missing": ["Xvnc"]}))
    monkeypatch.setattr(launcher, "resource_report", lambda path: {"ready": True, "reason": None})
    result = ScreenRuntime.doctor(directory)
    assert result == {"doctorVersion": 1, "ready": False, "reason": "DEPENDENCIES_MISSING",
                      "missing": ["Xvnc"], "resources": {"ready": True, "reason": None}}
    assert not directory.parent.exists()


def test_unsupported_doctor_never_reads_linux_resources(tmp_path, monkeypatch):
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(lambda: {
        "supported": False, "reason": "UNSUPPORTED_HOST", "missing": []}))
    monkeypatch.setattr(launcher, "resource_report", lambda path: pytest.fail("unsupported platform"))
    assert ScreenRuntime.doctor(tmp_path)["resources"] is None


def test_capabilities_require_optional_browser_dependency(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid: 1, raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: None)
    assert ScreenRuntime.capabilities() == {"supported": False, "reason": "DEPENDENCIES_MISSING", "missing": ["playwright"]}


def test_shutdown_in_progress_cannot_be_adopted_as_healthy(runtime, monkeypatch):
    manifest(runtime)
    runtime.authority.start("generation-1")
    runtime.authority.stop("generation-1")
    monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value))
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.start()


@pytest.mark.parametrize("expected", [None, "old-generation"])
def test_delayed_remote_start_cannot_adopt_or_restart_another_generation(runtime, monkeypatch, expected):
    manifest(runtime, phase="stopped", supervisor=None, children=[])
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("stale start must not spawn"))
    with pytest.raises(ScreenError, match="STALE_GENERATION"):
        runtime.start(expected_generation=expected)


@pytest.mark.parametrize("action", ["start", "stop"])
def test_identity_is_rechecked_after_waiting_for_lifecycle_lock(runtime, monkeypatch, action):
    manifest(runtime)
    runtime.authority.start("generation-1")
    def guard():
        assert runtime._lock.is_locked
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("wrong profile"))
    monkeypatch.setattr(launcher, "signal_process", lambda *a: pytest.fail("wrong profile"))
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        if action == "start":
            runtime.start(expected_generation="generation-1", identity_guard=guard)
        else:
            runtime.stop("generation-1", identity_guard=guard)
    assert runtime.authority.snapshot()["running"]


def test_dead_supervisor_never_launches_over_orphaned_browser(runtime, monkeypatch):
    manifest(runtime)
    monkeypatch.setattr(launcher, "is_alive", lambda value: value == {"pid": 124})
    assert runtime.status()["error"] == "RUNTIME_RECOVERY_REQUIRED"
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.start()


def test_interrupted_start_reservation_is_not_silently_replaced(runtime, monkeypatch):
    manifest(runtime, phase="starting", supervisor=None, children=[])
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.start()


def test_start_failure_never_releases_human_control(runtime, monkeypatch):
    manifest(runtime, phase="failed")
    runtime.authority.start("generation-1")
    runtime.authority.acquire("viewer", "generation-1", 1)
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        runtime.start()


def test_stop_refuses_human_and_active_run_before_signalling(runtime, monkeypatch):
    manifest(runtime)
    runtime.authority.start("generation-1")
    monkeypatch.setattr(launcher, "signal_process", lambda *args: pytest.fail("must not signal"))
    control = runtime.authority.acquire("viewer", "generation-1", 1)
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        runtime.stop("generation-1")
    runtime.authority.release("viewer", "generation-1", control["epoch"])
    admission = runtime.authority.begin_action("run", "action", "generation-1")
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        runtime.stop("generation-1")
    runtime.authority.end_action(admission)
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        runtime.stop("generation-1")


@pytest.mark.parametrize("raw", ['{', '[]', '{}', '{"schema":1}', 'x' * 17000])
def test_corrupt_state_fails_closed(runtime, raw):
    runtime.manifest.write_text(raw)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        runtime.read_manifest()


def test_state_and_socket_paths_are_private_and_bounded(runtime, tmp_path):
    state = manifest(runtime)
    assert json.loads(runtime.manifest.read_text()) == state
    assert runtime.manifest.stat().st_mode & 0o777 == 0o600
    # Do not write global /tmp paths in this unit test.
    private = private_directory(tmp_path / "private")
    assert private.stat().st_mode & 0o777 == 0o700


def test_symlink_private_directory_is_rejected(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    symlink = tmp_path / "link"
    symlink.symlink_to(victim, target_is_directory=True)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        private_directory(symlink)
    assert victim.stat().st_mode & 0o777 == 0o755


def test_symlink_manifest_is_rejected(runtime, tmp_path):
    target = tmp_path / "other"
    target.write_text('{}')
    runtime.manifest.symlink_to(target)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        runtime.read_manifest()


def test_pid_reuse_never_matches_previous_worker(monkeypatch):
    monkeypatch.setattr(launcher, "process_identity", lambda pid: dict(pid=pid, start="new", boot="boot"))
    assert not launcher.is_alive(dict(pid=123, start="old", boot="boot"))
    assert not launcher.is_alive(dict(pid=123, start="new", boot="other-boot"))
    assert launcher.is_alive(dict(pid=123, start="new", boot="boot"))


def test_signal_checks_identity_after_opening_pidfd(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid: read_fd, raising=False)
    monkeypatch.setattr(launcher, "is_alive", lambda identity: False)
    monkeypatch.setattr(launcher.signal, "pidfd_send_signal", lambda *args: pytest.fail("wrong PID"), raising=False)
    try:
        with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
            launcher.signal_process({"pid": 123}, 15)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)
