"""Private, persistent Linux browser runtime, independent of a viewer connection.

The gateway starts a detached supervisor and can adopt it after reconnect or
restart. Runtime metadata is internal: never return paths or CDP ports in RPCs.
Uncertain process ownership fails closed instead of killing an unrelated PID.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
import weakref
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from filelock import FileLock

from flowly.desktop_runtime.authority import SCHEMA as CONTROL_SCHEMA
from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.resources import resource_report

# The browser is resolved separately (install_recipe.screen_browser).
REQUIRED_PROGRAMS = ("Xvnc", "dbus-run-session", "python3", "bash",
                     "xfwm4", "xfsettingsd", "xfce4-panel", "xfce4-terminal",
                     "xfce4-settings-manager", "xfconf-query", "xdotool", "xprop")
_UNPINNED = object()


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ScreenError("RUNTIME_STATE_INVALID")
    path.chmod(0o700)
    return path


def write_state(path: Path, value: dict) -> None:
    temporary = path.with_name(".runtime-" + secrets.token_hex(12))
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as target:
            json.dump(value, target, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid: int, *, allow_init: bool = False) -> dict | None:
    """Kernel boot + start ticks distinguish reused PIDs; zombies are not live."""
    if type(pid) is not int or pid < 1 or (pid == 1 and not allow_init):
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return dict(pid=pid, start=fields[19],
                    boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    except (OSError, ValueError, IndexError):
        return None


def is_alive(identity: dict | None) -> bool:
    return isinstance(identity, dict) and process_identity(identity.get("pid")) == identity


def signal_process(identity: dict, signum: int) -> None:
    """pidfd keeps the signal tied to the exact process even during exit/reuse."""
    try:
        fd = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return
    try:
        if not is_alive(identity):
            raise ScreenError("RUNTIME_STATE_INVALID")
        signal.pidfd_send_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


class ScreenRuntime:
    START_TIMEOUT = 30
    STOP_TIMEOUT = 15

    def __init__(self, directory: Path, *, create: bool = True, require_parent: bool = False):
        from flowly.desktop_runtime.profile_lifecycle import assert_not_retired, construction_guard

        self.directory = directory.absolute()
        # Existing supervisor adoption cannot wait behind a deleting profile
        # that itself waits for the launcher's lifecycle lock. It never creates
        # the runtime directory; the parent has already reserved that directory.
        with construction_guard(self.directory) if create else nullcontext():
            try:
                if create:
                    assert_not_retired(self.directory)
                if require_parent:
                    parent = self.directory.parent.lstat()
                    if not stat.S_ISDIR(parent.st_mode):
                        raise ValueError()
                if create and os.name == "posix":
                    private_directory(self.directory)
                elif create:
                    self.directory.mkdir(parents=True, exist_ok=True)
                info = self.directory.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError()
            except (OSError, ValueError):
                raise ScreenError("PROFILE_IDENTITY_CHANGED") from None
            self._directory_identity = (info.st_dev, info.st_ino)
            if os.name == "posix":
                # Retain the inode while this object exists. Removal/recreation
                # cannot recycle it and make a stale controller look current.
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                opened = os.fstat(descriptor)
                self._directory_identity = (opened.st_dev, opened.st_ino)
                weakref.finalize(self, os.close, descriptor)
            self.manifest = self.directory / "runtime.json"
            self.authority = ScreenAuthority(self.directory / "authority")
            self._lock = FileLock(self.directory / "lifecycle.lock", timeout=45, mode=0o600)

    def assert_current_directory(self) -> None:
        try:
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != self._directory_identity):
                raise ValueError()
        except (OSError, ValueError):
            raise ScreenError("PROFILE_IDENTITY_CHANGED") from None

    @staticmethod
    def capabilities() -> dict:
        if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
            return dict(supported=False, reason="UNSUPPORTED_HOST", missing=[])
        # Programs an interrupted installation left may be half-configured:
        # no screen until a later installation repairs them.
        from flowly.desktop_runtime.install_jobs import environment_installation_uncertain

        try:
            if environment_installation_uncertain():
                return dict(supported=False, reason="INSTALL_RECOVERY_REQUIRED", missing=[])
        except ScreenError as exc:
            return dict(supported=False, reason=exc.code, missing=[])
        missing = [program for program in REQUIRED_PROGRAMS if shutil.which(program) is None]
        from flowly.desktop_runtime.install_recipe import screen_browser

        if screen_browser() is None:
            missing.append("chromium")
        if importlib.util.find_spec("playwright") is None:
            missing.append("playwright")
        if missing:
            return dict(supported=False, reason="DEPENDENCIES_MISSING", missing=missing)
        # Chromium's sandbox requires an unprivileged runtime account. Never
        # silently add --no-sandbox to make a root installation appear healthy.
        if os.geteuid() == 0:
            from flowly.desktop_runtime.runtime_account import runtime_account

            try:
                runtime_account()
            except ScreenError as exc:
                return dict(supported=False, reason=exc.code, missing=[])
            if shutil.which("dbus-daemon", path="/usr/bin:/bin") is None:
                return dict(supported=False, reason="DEPENDENCIES_MISSING", missing=["dbus-daemon"])
            # Guards each profile browser's DevTools port (devtools_guard).
            if shutil.which("nft", path="/usr/sbin:/sbin") is None:
                return dict(supported=False, reason="DEPENDENCIES_MISSING", missing=["nft"])
        return dict(supported=True, reason=None, missing=[])

    @staticmethod
    def resource_directory(directory: Path) -> Path:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            from flowly.desktop_runtime.runtime_account import ROOT

            return ROOT
        return directory

    @staticmethod
    def doctor(directory: Path) -> dict:
        """Inspect an uncreated profile without constructing a runtime or authority."""
        capability = ScreenRuntime.capabilities()
        resources = resource_report(ScreenRuntime.resource_directory(directory)) if capability["reason"] != "UNSUPPORTED_HOST" else None
        return {"doctorVersion": 1, "ready": capability["supported"] and bool(resources and resources["ready"]),
                "reason": capability["reason"] or (resources["reason"] if resources else "RESOURCE_CHECK_UNAVAILABLE"),
                "missing": capability["missing"], "resources": resources}

    def _startup_lock(self) -> FileLock:
        # Serialize profile starts for this runtime account until the new browser
        # is ready and its memory is visible. Other accounts/external allocations
        # remain outside this budget check; it is not a memory reservation.
        parent = private_directory(Path("/tmp") / f"flowly-screen-{os.getuid()}")
        return FileLock(parent / "startup.lock", timeout=15, mode=0o600)

    @property
    def socket_directory(self) -> Path:
        # Unix sockets have a short path limit even when FLOWLY_HOME is long.
        parent = private_directory(Path("/tmp") / f"flowly-screen-{os.getuid()}")
        key = hashlib.sha256(os.fsencode(self.directory)).hexdigest()[:24]
        return private_directory(parent / key)

    @property
    def application_socket_directory(self) -> Path:
        if os.geteuid() == 0:
            from flowly.desktop_runtime.application_layout import root_application_layout

            return root_application_layout(self.directory).sockets
        # Keep Xvnc/X authority separate from the agent's input/observer brokers.
        parent = private_directory(Path("/tmp") / f"flowly-screen-{os.getuid()}")
        key = hashlib.sha256(os.fsencode(self.directory)).hexdigest()[:24]
        return private_directory(parent / (key + "-apps"))

    def rfb_socket_for(self, state: dict) -> Path:
        version = state.get("socketLayoutVersion", 1)
        if type(version) is not int or version not in (1, 2, 3):
            raise ScreenError("RUNTIME_STATE_INVALID")
        if version == 3:
            from flowly.desktop_runtime.application_layout import root_application_layout

            return root_application_layout(self.directory).sockets / "rfb.sock"
        if version == 2 and os.geteuid() == 0:
            raise ScreenError("RUNTIME_STATE_INVALID")
        # Existing supervisors keep their original socket until explicit stop.
        # Never follow a path supplied by a manifest or public RPC parameter.
        directory = self.application_socket_directory if version == 2 else self.socket_directory
        return directory / "rfb.sock"

    def read_manifest(self) -> dict | None:
        self.assert_current_directory()
        try:
            fd = os.open(self.manifest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd) as source:
                value = json.loads(source.read(16385))
            if (not isinstance(value, dict) or value.get("schema") != 1
                    or value.get("phase") not in {"starting", "running", "stopped", "failed"}
                    or not isinstance(value.get("generation"), str)
                    or not isinstance(value.get("children"), list)):
                raise ValueError()
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ScreenError("RUNTIME_STATE_INVALID") from exc

    def status(self) -> dict | None:
        state = self.read_manifest()
        if state and state["phase"] in {"starting", "running"}:
            if not is_alive(state.get("supervisor")):
                return {**state, "phase": "failed", "error": "RUNTIME_RECOVERY_REQUIRED"}
            if state["phase"] == "running" and not all(is_alive(child) for child in state["children"]):
                return {**state, "phase": "failed", "error": "RUNTIME_FAILED"}
            if self._outdated(state):
                return {**state, "phase": "failed", "error": "SCREEN_RESTART_REQUIRED"}
        return state

    @staticmethod
    def _outdated(state: dict) -> bool:
        """A live screen whose own services speak another control schema.

        Its input and observer services read and write the control record
        with the code the screen was started with. A Core update restarts the
        gateway, not the screen: a screen started before 2026-10-20 could not
        read the record the updated gateway wrote, and every takeover and view
        failed as "connection unavailable". Such a screen is not shared; it is
        restarted (stop, then start), by the agent when nobody holds it, by an
        explicit recovery otherwise. Its supervisor records the schema it
        speaks when it starts; one that recorded none predates the record.
        """
        return (state["phase"] == "running" and is_alive(state.get("supervisor"))
                and state.get("controlProtocol") != CONTROL_SCHEMA)

    def _died_unheld(self, state: dict) -> bool:
        """A running screen whose supervisor and every child are gone, that no
        person holds and no live task claims (a dead task's claim is dropped
        when the record is read). An orphaned browser, an interrupted start or
        a person's claim still waits for an explicit recovery."""
        if state["phase"] != "running" or state.get("supervisor") is None:
            return False
        if is_alive(state.get("supervisor")) or any(is_alive(child) for child in state["children"]):
            return False
        control = self.authority.snapshot()
        return control["control"] == "agent" and control["runId"] is None

    def _end_outdated(self, state: dict) -> None:
        """Kill an outdated screen's supervisor; the dead-screen path does the rest.

        A graceful stop would run its old code against the current record.
        """
        signal_process(state["supervisor"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while is_alive(state["supervisor"]):
            if time.monotonic() > deadline:
                raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
            time.sleep(0.05)

    def start(self, *, expected_generation: str | None | object = _UNPINNED,
              identity_guard: Callable[[], None] | None = None) -> dict:
        # Parent holds activity until the detached supervisor owns its lease;
        # an installer cannot enter the gap between admission and readiness.
        from flowly.desktop_runtime.installation_gate import environment_activity

        capabilities = self.capabilities()
        if not capabilities["supported"]:
            raise ScreenError(capabilities["reason"])
        with environment_activity():
            from flowly.desktop_runtime.install_jobs import assert_environment_installation_idle

            assert_environment_installation_idle()
            return self._start_with_activity(expected_generation=expected_generation, identity_guard=identity_guard)

    def _start_with_activity(self, *, expected_generation: str | None | object = _UNPINNED,
                             identity_guard: Callable[[], None] | None = None) -> dict:
        from flowly.desktop_runtime.profile_lifecycle import assert_not_retired

        self.assert_current_directory()
        assert_not_retired(self.directory)
        with self._lock:
            self.assert_current_directory()
            assert_not_retired(self.directory)
            if identity_guard:
                identity_guard()
            existing = self.read_manifest()
            # Remote starts are compare-and-start operations. A delayed request
            # must not restart a runtime the user stopped or replaced meanwhile.
            if expected_generation is not _UNPINNED and expected_generation != (existing["generation"] if existing else None):
                raise ScreenError("STALE_GENERATION")
            if existing and existing["phase"] == "running" and self.status()["phase"] == "running":
                control = self.authority.snapshot()
                if not control["running"] or control["generation"] != existing["generation"]:
                    raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
                return existing
            if existing and existing["phase"] == "starting" and is_alive(existing.get("supervisor")):
                return self._await_start(existing["generation"])
            if existing and self._outdated(existing):
                # Nobody holds it (no person, no live task): restart it now
                # instead of refusing the task that needs a browser.
                control = self.authority.snapshot()
                if control["control"] != "agent" or control["runId"] is not None:
                    raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
                self._end_outdated(existing)
                self._finish_dead(existing, existing["generation"], self.authority.snapshot())
                existing = self.read_manifest()
            if existing and self._died_unheld(existing):
                # Its processes died with the machine (a restart, a new cloud
                # image, a crash, the OOM killer): nothing of it runs and
                # nobody holds it. Finish it and start anew, instead of
                # refusing every task until someone presses Recover (owner's
                # cloud agent after an image update, 2026-10-24).
                self._finish_dead(existing, existing["generation"], self.authority.snapshot())
                existing = self.read_manifest()
            if existing and (is_alive(existing.get("supervisor"))
                             or any(is_alive(child) for child in existing["children"])
                             or existing["phase"] in {"starting", "running"}):
                raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
            control = self.authority.snapshot()
            if control["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if control["runId"] is not None:
                raise ScreenError("SCREEN_BUSY")
            with self._startup_lock():
                if identity_guard:
                    identity_guard()
                resources = resource_report(self.resource_directory(self.directory))
                if not resources["ready"]:
                    raise ScreenError(resources["reason"])
                return self._spawn()

    def _spawn(self) -> dict:
        """Called with both profile lifecycle and account startup locks held."""
        root = os.geteuid() == 0
        if root:
            from flowly.desktop_runtime.application_layout import root_application_layout

            root_application_layout(self.directory, create=True)
        generation = secrets.token_hex(16)
        write_state(self.manifest, dict(schema=1, socketLayoutVersion=3 if root else 2, generation=generation, phase="starting",
                                       supervisor=None, children=[], error=None))
        # No parent pipe survives. Gateway/viewer disconnects do not end the
        # browser, and a restarted gateway adopts its original generation.
        try:
            from flowly.desktop_runtime.resource_scope import scoped_command

            command = [sys.executable, "-m", "flowly.desktop_runtime.supervisor", str(self.directory), generation]
            command, scoped = scoped_command(command if root else ["dbus-run-session", "--", *command],
                                              self.directory, generation)
            state = self.read_manifest()
            write_state(self.manifest, {**state, "resourceScope": scoped})
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except (OSError, ScreenError) as exc:
            write_state(self.manifest, dict(schema=1, generation=generation, phase="failed",
                                           supervisor=None, children=[], error="RUNTIME_START_FAILED"))
            raise ScreenError("RUNTIME_START_FAILED") from exc
        try:
            return self._await_start(generation, process)
        finally:
            # Popen instances are reaped by subprocess on the next spawn;
            # do not wait for a deliberately persistent healthy worker.
            process.poll()

    def _await_start(self, generation: str, process: subprocess.Popen | None = None) -> dict:
        deadline = time.monotonic() + self.START_TIMEOUT
        while time.monotonic() < deadline:
            state = self.read_manifest()
            if state is None or state["generation"] != generation:
                raise ScreenError("RUNTIME_STATE_INVALID")
            if state["phase"] == "running":
                return state
            if state["phase"] == "failed" or (process is not None and process.poll() is not None):
                # The one start failure the person can act on is named.
                raise ScreenError("SCREEN_ISOLATION_UNAVAILABLE" if state.get("error") == "SCREEN_ISOLATION_UNAVAILABLE"
                                  else "RUNTIME_START_FAILED")
            time.sleep(0.05)
        # A timeout is not permission to launch another worker. Leave the
        # reservation intact so a later request adopts it or reports recovery.
        raise ScreenError("RUNTIME_START_TIMEOUT")

    @staticmethod
    def _terminate_recorded(children: list) -> None:
        """TERM, then KILL, only the exact recorded processes (pidfd + identity)."""
        for signum, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            for child in children:
                if is_alive(child):
                    try:
                        signal_process(child, signum)
                    except ScreenError:
                        pass  # Exited or replaced between the checks.
            deadline = time.monotonic() + grace
            while any(is_alive(child) for child in children) and time.monotonic() < deadline:
                time.sleep(0.05)

    def _reap_account_processes(self) -> bool:
        """Kill every process of this layout's dedicated account (root hosts).

        Returns False when no dedicated account identifies them: a legacy
        shared-account layout or an unprivileged host needs explicit recovery.
        """
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return False
        from flowly.desktop_runtime.application_layout import root_application_layout
        from flowly.desktop_runtime.profile_accounts import terminate_profile_account_processes

        layout = root_application_layout(self.directory)
        if layout.account_name is None:
            return False
        terminate_profile_account_processes(layout.account_name)
        return True

    def _finish_dead(self, state: dict, generation: str, control: dict) -> None:
        """End a generation whose supervisor is gone (crashed, or killed as outdated)."""
        if state.get("resourceScope") is True:
            from flowly.desktop_runtime.resource_scope import stop_scope

            stop_scope(self.directory, generation)
        if any(is_alive(child) for child in state["children"]):
            # A crashed supervisor left applications behind. A
            # dedicated-account layout identifies every one of them;
            # otherwise only the recorded, identity-checked children.
            if not self._reap_account_processes():
                self._terminate_recorded(state["children"])
            if any(is_alive(child) for child in state["children"]):
                raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
        if control["running"] and control["generation"] == generation:
            # Nothing of this generation runs; its human/run claims
            # guard nothing and would otherwise block every restart.
            self.authority.stop_dead(generation)
        if state["phase"] != "stopped":
            # The supervisor died without recording its stop. Nothing
            # of this generation runs now, so finish it; otherwise the
            # stale "running" record would refuse every later start.
            write_state(self.manifest, {**state, "phase": "stopped", "supervisor": None,
                                        "children": [], "error": None})

    def stop(self, generation: str, *, identity_guard: Callable[[], None] | None = None,
             idle_revision: int | None = None) -> None:
        self.assert_current_directory()
        with self._lock:
            self.assert_current_directory()
            if identity_guard:
                identity_guard()
            state = self.read_manifest()
            if state is None or state["generation"] != generation:
                raise ScreenError("STALE_GENERATION")
            control = self.authority.snapshot()
            if self._outdated(state):
                if idle_revision is not None and (control["control"] != "agent" or control["runId"] is not None):
                    raise ScreenError("SCREEN_BUSY")
                self._end_outdated(state)
                self._finish_dead(state, generation, control)
                return
            if not is_alive(state.get("supervisor")):
                if idle_revision is not None:
                    # Recovery ends human claims; only an explicit stop may.
                    raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
                self._finish_dead(state, generation, control)
                return
            if control["running"] or idle_revision is not None:
                self.authority.stop(generation, idle_revision=idle_revision)
            signal_process(state["supervisor"], signal.SIGTERM)
            deadline = time.monotonic() + self.STOP_TIMEOUT
            while time.monotonic() < deadline:
                current = self.read_manifest()
                if current and current["phase"] == "stopped" and not is_alive(state["supervisor"]):
                    # Terminal jobs may have daemonized out of the supervisor's
                    # process tree; none may outlive the profile's screen.
                    self._reap_account_processes()
                    if state.get("resourceScope") is True:
                        from flowly.desktop_runtime.resource_scope import stop_scope

                        stop_scope(self.directory, generation)
                    if idle_revision is not None:
                        # Tells the user why the screen stopped by itself.
                        write_state(self.manifest, {**current, "error": "IDLE_STOPPED"})
                    return
                if current and current["phase"] == "failed" and not is_alive(state["supervisor"]):
                    raise ScreenError("RUNTIME_STOP_FAILED")
                time.sleep(0.05)
            raise ScreenError("RUNTIME_STOP_TIMEOUT")
