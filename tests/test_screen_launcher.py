"""Runtime admission, private state and process identity safety."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.desktop_runtime import launcher
from flowly.desktop_runtime.authority import SCHEMA as CONTROL_SCHEMA
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime, private_directory, write_state


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(
        lambda: dict(supported=True, reason=None, missing=[])))
    return ScreenRuntime(tmp_path / "desktop")


def manifest(runtime, **updates):
    # A screen started by this code: its supervisor records the control schema it speaks.
    value = dict(schema=1, generation="generation-1", phase="running", supervisor={"pid": 123},
                 children=[{"pid": 124}], error=None, controlProtocol=CONTROL_SCHEMA)
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


def test_application_sockets_are_separate_and_legacy_paths_remain_pinned(runtime):
    broker = runtime.socket_directory
    applications = runtime.application_socket_directory
    assert broker != applications and broker.parent == applications.parent
    assert applications.stat().st_mode & 0o777 == 0o700
    assert runtime.rfb_socket_for({"socket": "/private/untrusted.sock"}) == broker / "rfb.sock"
    assert runtime.rfb_socket_for({"socketLayoutVersion": 2, "socket": "/private/untrusted.sock"}) == applications / "rfb.sock"
    other = ScreenRuntime(runtime.directory.parent / "other")
    assert other.application_socket_directory not in (broker, applications)


@pytest.mark.parametrize("version", [None, True, "2", 0, 4, {}, []])
def test_unknown_socket_layout_is_not_a_path_fallback(runtime, version):
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        runtime.rfb_socket_for({"socketLayoutVersion": version})


@pytest.mark.parametrize("code", ["UNPRIVILEGED_RUNTIME_REQUIRED", "RUNTIME_STATE_INVALID"])
def test_root_capability_requires_valid_account_without_provisioning(monkeypatch, code):
    from flowly.desktop_runtime import runtime_account as accounts

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid: None, raising=False)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcher.shutil, "which", lambda *a, **kw: "/usr/bin/test-program")
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: object())

    def verify():
        raise ScreenError(code)

    monkeypatch.setattr(accounts, "runtime_account", verify)
    assert ScreenRuntime.capabilities() == {"supported": False, "reason": code, "missing": []}


def test_root_capability_requires_private_bus_program(monkeypatch):
    from flowly.desktop_runtime import runtime_account as accounts

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid: None, raising=False)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcher.shutil, "which", lambda name, **kw: None if name == "dbus-daemon" else "/usr/bin/test")
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(accounts, "runtime_account", lambda: accounts.RuntimeAccount(991, 992))
    assert ScreenRuntime.capabilities() == {"supported": False, "reason": "DEPENDENCIES_MISSING", "missing": ["dbus-daemon"]}


def test_root_budget_uses_actual_application_filesystem(monkeypatch, tmp_path):
    from flowly.desktop_runtime.runtime_account import ROOT

    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(lambda: {
        "supported": True, "reason": None, "missing": []}))
    inspected = []

    def report(path):
        inspected.append(path)
        return {"ready": True, "reason": None}

    monkeypatch.setattr(launcher, "resource_report", report)
    assert ScreenRuntime.doctor(tmp_path / "does-not-exist")["ready"]
    assert inspected == [ROOT]
    assert not list(tmp_path.iterdir())


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
    monkeypatch.setattr(launcher.shutil, "which", lambda name, **kw: "/usr/bin/" + name)
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


def test_a_screen_that_died_with_the_machine_starts_again_when_nobody_holds_it(runtime, monkeypatch):
    """A restart or a new cloud image kills the supervisor and every child;
    the record still says running. The agent's next start finishes it and
    starts anew instead of answering RUNTIME_RECOVERY_REQUIRED forever."""
    manifest(runtime)
    runtime.authority.start("generation-1")
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    monkeypatch.setattr(launcher, "signal_process", lambda *args: pytest.fail("nothing runs to signal"))
    monkeypatch.setattr(launcher, "resource_report", lambda path: {"ready": True})
    monkeypatch.setattr(runtime, "_spawn", lambda: {"generation": "new"})
    assert runtime.status()["error"] == "RUNTIME_RECOVERY_REQUIRED"
    assert runtime.start() == {"generation": "new"}
    assert json.loads(runtime.manifest.read_text())["phase"] == "stopped"
    control = runtime.authority.snapshot()
    assert (control["running"], control["control"], control["runId"]) == (False, "agent", None)


def test_a_dead_screen_the_person_holds_waits_for_an_explicit_recovery(runtime, monkeypatch):
    manifest(runtime)
    runtime.authority.start("generation-1")
    runtime.authority.acquire("viewer", "generation-1", runtime.authority.snapshot()["epoch"])
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    monkeypatch.setattr(runtime, "_spawn", lambda: pytest.fail("the person holds it"))
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.start()
    assert json.loads(runtime.manifest.read_text())["phase"] == "running"


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


@pytest.mark.parametrize("scoped", [False, True])
def test_stop_refuses_human_and_active_run_before_signalling(runtime, monkeypatch, scoped):
    from flowly.desktop_runtime import resource_scope

    manifest(runtime, resourceScope=scoped)
    monkeypatch.setattr(resource_scope, "stop_scope", lambda *args: pytest.fail("must not stop scope"))
    runtime.authority.start("generation-1")
    monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value))
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



def test_stop_of_a_dead_generation_ends_its_human_and_run_claims(runtime, monkeypatch):
    manifest(runtime)
    runtime.authority.start("generation-1")
    admission = runtime.authority.begin_action("run", "action", "generation-1")
    runtime.authority.end_action(admission)
    runtime.authority.acquire("viewer", "generation-1", runtime.authority.snapshot()["epoch"])
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    monkeypatch.setattr(launcher, "signal_process", lambda *args: pytest.fail("nothing runs to signal"))
    runtime.stop("generation-1")
    control = runtime.authority.snapshot()
    assert (control["running"], control["control"], control["runId"]) == (False, "agent", None)
    assert json.loads(runtime.manifest.read_text())["phase"] == "stopped"
    with pytest.raises(ScreenError, match="STALE_GENERATION"):
        runtime.stop("generation-0")


@pytest.mark.parametrize("scope_failure", [False, True])
def test_dead_scoped_runtime_reaps_orphans_before_ending_human_claims(runtime, monkeypatch, scope_failure):
    from flowly.desktop_runtime import resource_scope

    generation = "a" * 32
    manifest(runtime, generation=generation, resourceScope=True)
    runtime.authority.start(generation)
    runtime.authority.acquire("viewer", generation, 1)
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    calls = []

    def reap(directory, observed_generation):
        assert runtime.authority.snapshot()["control"] == "human"
        calls.append((directory, observed_generation))
        if scope_failure:
            raise ScreenError("RUNTIME_STOP_FAILED")

    monkeypatch.setattr(resource_scope, "stop_scope", reap)
    # An idle timer must not use dead-generation recovery to end human control.
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.stop(generation, idle_revision=runtime.authority.snapshot()["revision"])
    assert not calls
    if scope_failure:
        with pytest.raises(ScreenError, match="RUNTIME_STOP_FAILED"):
            runtime.stop(generation)
        assert runtime.authority.snapshot()["control"] == "human"
        assert runtime.read_manifest()["phase"] == "running"
    else:
        runtime.stop(generation)
        assert not runtime.authority.snapshot()["running"]
        assert runtime.read_manifest()["phase"] == "stopped"
    assert calls == [(runtime.directory, generation)]


def test_idle_stop_is_refused_after_any_change_and_never_recovers_a_dead_screen(runtime, monkeypatch):
    manifest(runtime)
    runtime.authority.start("generation-1")
    observed = runtime.authority.snapshot()["revision"]
    control = runtime.authority.acquire("viewer", "generation-1", runtime.authority.snapshot()["epoch"])
    runtime.authority.release("viewer", "generation-1", control["epoch"])
    monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value))
    monkeypatch.setattr(launcher, "signal_process", lambda *args: pytest.fail("used since observed"))
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        runtime.stop("generation-1", idle_revision=observed)
    assert runtime.authority.snapshot()["running"]
    runtime.authority.acquire("viewer", "generation-1", runtime.authority.snapshot()["epoch"])
    monkeypatch.setattr(launcher, "is_alive", lambda value: False)
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.stop("generation-1", idle_revision=runtime.authority.snapshot()["revision"])
    assert runtime.authority.snapshot()["control"] == "human", "only an explicit stop ends human claims"


def test_idle_stop_records_why_the_screen_stopped(runtime, monkeypatch):
    state = manifest(runtime)
    runtime.authority.start("generation-1")
    alive = {"supervisor": True}
    monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value) and alive["supervisor"])

    def graceful(identity, signum):
        alive["supervisor"] = False
        write_state(runtime.manifest, {**state, "phase": "stopped", "supervisor": None, "children": []})

    monkeypatch.setattr(launcher, "signal_process", graceful)
    runtime.stop("generation-1", idle_revision=runtime.authority.snapshot()["revision"])
    assert not runtime.authority.snapshot()["running"]
    assert runtime.status()["phase"] == "stopped" and runtime.status()["error"] == "IDLE_STOPPED"


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


@pytest.mark.parametrize("answer,reason", [
    (True, "INSTALL_RECOVERY_REQUIRED"),
    (ScreenError("INSTALL_STATE_INVALID"), "INSTALL_STATE_INVALID"),
])
def test_a_screen_is_not_offered_over_an_unfinished_installation(monkeypatch, answer, reason):
    """Half-installed programs may be on disk; the screen waits for a repair."""
    from flowly.desktop_runtime import install_jobs

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid: 1, raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name, **kw: "/usr/bin/" + name)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 1000)
    def uncertain():
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(install_jobs, "environment_installation_uncertain", uncertain)
    assert ScreenRuntime.capabilities() == {"supported": False, "reason": reason, "missing": []}
    monkeypatch.setattr(install_jobs, "environment_installation_uncertain", lambda: False)
    assert ScreenRuntime.capabilities()["supported"] is True



class _Processes:
    """Which recorded processes live; a SIGKILL ends the one it names."""

    def __init__(self, monkeypatch, *pids):
        import signal

        self.alive = set(pids)
        self.signals: list = []
        monkeypatch.setattr(launcher, "is_alive", lambda value: bool(value) and value.get("pid") in self.alive)

        def signal_process(identity, signum):
            self.signals.append((identity["pid"], signum))
            if signum == signal.SIGKILL:
                self.alive.discard(identity["pid"])

        monkeypatch.setattr(launcher, "signal_process", signal_process)


def outdated(runtime):
    """A screen started before its supervisor recorded the control schema
    (the test VPS, 2026-10-20): its own services cannot read today's record."""
    value = manifest(runtime)
    value.pop("controlProtocol")
    write_state(runtime.manifest, value)
    runtime.authority.start("generation-1")
    return value


def test_a_screen_speaking_another_control_schema_is_reported_for_restart(runtime, monkeypatch):
    _Processes(monkeypatch, 123, 124)
    outdated(runtime)
    status = runtime.status()
    assert (status["phase"], status["error"]) == ("failed", "SCREEN_RESTART_REQUIRED")
    write_state(runtime.manifest, {**json.loads(runtime.manifest.read_text()), "controlProtocol": CONTROL_SCHEMA - 1})
    assert runtime.status()["error"] == "SCREEN_RESTART_REQUIRED"
    write_state(runtime.manifest, {**json.loads(runtime.manifest.read_text()), "controlProtocol": CONTROL_SCHEMA})
    assert runtime.status()["phase"] == "running"


def test_the_agent_restarts_an_outdated_screen_nobody_holds(runtime, monkeypatch):
    import signal

    processes = _Processes(monkeypatch, 123, 124)
    outdated(runtime)
    monkeypatch.setattr(runtime, "_reap_account_processes", lambda: processes.alive.discard(124) or True)
    monkeypatch.setattr(launcher, "resource_report", lambda path: {"ready": True})
    monkeypatch.setattr(runtime, "_spawn", lambda: {"generation": "new"})
    assert runtime.start() == {"generation": "new"}
    assert processes.signals == [(123, signal.SIGKILL)]  # never a graceful stop in old code
    control = runtime.authority.snapshot()
    assert (control["running"], control["control"], control["runId"]) == (False, "agent", None)


def test_an_outdated_screen_the_person_holds_waits_for_an_explicit_recovery(runtime, monkeypatch):
    processes = _Processes(monkeypatch, 123, 124)
    outdated(runtime)
    monkeypatch.setattr(runtime, "_reap_account_processes", lambda: processes.alive.discard(124) or True)
    monkeypatch.setattr(runtime, "_spawn", lambda: pytest.fail("the person holds it"))
    runtime.authority.acquire("viewer", "generation-1", runtime.authority.snapshot()["epoch"])
    with pytest.raises(ScreenError, match="RUNTIME_RECOVERY_REQUIRED"):
        runtime.start()
    assert processes.signals == []
    # An idle stop never ends what a person holds.
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        runtime.stop("generation-1", idle_revision=runtime.authority.snapshot()["revision"])
    assert processes.signals == []
    # "Recover screen" (an explicit stop) ends it, the person's claim with it.
    runtime.stop("generation-1")
    control = runtime.authority.snapshot()
    assert (control["running"], control["control"]) == (False, "agent")
    assert json.loads(runtime.manifest.read_text())["phase"] == "stopped" and 123 not in processes.alive


def test_an_unused_outdated_screen_still_idle_stops(runtime, monkeypatch):
    processes = _Processes(monkeypatch, 123, 124)
    outdated(runtime)
    monkeypatch.setattr(runtime, "_reap_account_processes", lambda: processes.alive.discard(124) or True)
    runtime.stop("generation-1", idle_revision=runtime.authority.snapshot()["revision"])
    assert json.loads(runtime.manifest.read_text())["phase"] == "stopped"
