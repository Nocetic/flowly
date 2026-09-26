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
from pathlib import Path
from typing import Callable

from filelock import FileLock

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.resources import resource_report

REQUIRED_PROGRAMS = ("Xvnc", "openbox", "dbus-run-session", "chromium")
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

    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        if os.name == "posix":
            private_directory(self.directory)
        self.manifest = self.directory / "runtime.json"
        self.authority = ScreenAuthority(self.directory / "authority")
        self._lock = FileLock(self.directory / "lifecycle.lock", timeout=45, mode=0o600)

    @staticmethod
    def capabilities() -> dict:
        if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
            return dict(supported=False, reason="UNSUPPORTED_HOST", missing=[])
        missing = [program for program in REQUIRED_PROGRAMS if shutil.which(program) is None]
        if importlib.util.find_spec("playwright") is None:
            missing.append("playwright")
        if missing:
            return dict(supported=False, reason="DEPENDENCIES_MISSING", missing=missing)
        # Chromium's sandbox requires an unprivileged runtime account. Never
        # silently add --no-sandbox to make a root installation appear healthy.
        if os.geteuid() == 0:
            return dict(supported=False, reason="UNPRIVILEGED_RUNTIME_REQUIRED", missing=[])
        return dict(supported=True, reason=None, missing=[])

    @staticmethod
    def doctor(directory: Path) -> dict:
        """Inspect an uncreated profile without constructing a runtime or authority."""
        capability = ScreenRuntime.capabilities()
        resources = resource_report(directory) if capability["reason"] != "UNSUPPORTED_HOST" else None
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

    def read_manifest(self) -> dict | None:
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
        return state

    def start(self, *, expected_generation: str | None | object = _UNPINNED,
              identity_guard: Callable[[], None] | None = None) -> dict:
        capabilities = self.capabilities()
        if not capabilities["supported"]:
            raise ScreenError(capabilities["reason"])
        private_directory(self.directory)
        with self._lock:
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
                resources = resource_report(self.directory)
                if not resources["ready"]:
                    raise ScreenError(resources["reason"])
                return self._spawn()

    def _spawn(self) -> dict:
        """Called with both profile lifecycle and account startup locks held."""
        generation = secrets.token_hex(16)
        write_state(self.manifest, dict(schema=1, generation=generation, phase="starting",
                                       supervisor=None, children=[], error=None))
        # No parent pipe survives. Gateway/viewer disconnects do not end the
        # browser, and a restarted gateway adopts its original generation.
        try:
            process = subprocess.Popen(
                ["dbus-run-session", "--", sys.executable, "-m", "flowly.desktop_runtime.supervisor",
                 str(self.directory), generation],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except OSError as exc:
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
                raise ScreenError("RUNTIME_START_FAILED")
            time.sleep(0.05)
        # A timeout is not permission to launch another worker. Leave the
        # reservation intact so a later request adopts it or reports recovery.
        raise ScreenError("RUNTIME_START_TIMEOUT")

    def stop(self, generation: str, *, identity_guard: Callable[[], None] | None = None) -> None:
        with self._lock:
            if identity_guard:
                identity_guard()
            state = self.read_manifest()
            if state is None or state["generation"] != generation:
                raise ScreenError("STALE_GENERATION")
            control = self.authority.snapshot()
            if control["running"]:
                self.authority.stop(generation)
            if not is_alive(state.get("supervisor")):
                if any(is_alive(child) for child in state["children"]):
                    raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
                return
            signal_process(state["supervisor"], signal.SIGTERM)
            deadline = time.monotonic() + self.STOP_TIMEOUT
            while time.monotonic() < deadline:
                current = self.read_manifest()
                if current and current["phase"] == "stopped" and not is_alive(state["supervisor"]):
                    return
                if current and current["phase"] == "failed" and not is_alive(state["supervisor"]):
                    raise ScreenError("RUNTIME_STOP_FAILED")
                time.sleep(0.05)
            raise ScreenError("RUNTIME_STOP_TIMEOUT")
