"""Runtime admission, private state and process identity safety."""

import json
import os

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
    assert runtime.start() == expected
    # Human control survives controller reconnect too.
    runtime.authority.acquire("viewer", "generation-1", 1)
    assert runtime.start() == expected
    assert runtime.authority.snapshot()["control"] == "human"


def test_shutdown_in_progress_cannot_be_adopted_as_healthy(runtime, monkeypatch):
    manifest(runtime)
    runtime.authority.start("generation-1")
    runtime.authority.stop("generation-1")
    monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value))
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.start()


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
