"""Coordinate profile removal with screen construction and detached runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path

from filelock import FileLock

from flowly.desktop_runtime.authority import ScreenError


def _guard_path(directory: Path) -> Path:
    from flowly.desktop_runtime.launcher import private_directory

    parent = private_directory(Path("/tmp") / f"flowly-screen-{os.getuid()}")
    key = hashlib.sha256(os.fsencode(directory.absolute())).hexdigest()[:24]
    return parent / (key + "-profile.lock")


def construction_guard(directory: Path):
    if os.name != "posix":
        return nullcontext()  # These platforms do not host a screen runtime.
    # This lock survives removal of the profile and its short socket directory.
    return FileLock(_guard_path(directory), timeout=45, mode=0o600, is_singleton=True)


def _retirement_path(directory: Path) -> Path:
    # Keep crash-recovery state alongside the profile collection, outside the
    # profile being removed. /tmp is suitable for process locks, not a deletion
    # journal: reboot may clear it after storage removal but before the receipt.
    directory = directory.absolute()
    parent = directory.parent.parent / ".flowly-screen-retirements"
    try:
        info = parent.lstat()
    except FileNotFoundError:
        pass  # Read-only checks must not create storage.
    else:
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ScreenError("RUNTIME_STATE_INVALID")
    key = hashlib.sha256(os.fsencode(directory)).hexdigest()
    return parent / (key + ".json")


def _prepare_retirement_path(directory: Path) -> Path:
    path = _retirement_path(directory)
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        fd = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return _retirement_path(directory)  # Reject links/unsafe concurrently-created entries.


def retirement(directory: Path) -> dict | None:
    if os.name != "posix":
        return None
    try:
        fd = os.open(_retirement_path(directory), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ScreenError("RUNTIME_STATE_INVALID") from None
    try:
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError()
            raw = source.read(1025)
        value = json.loads(raw)
        if (len(raw) > 1024 or not isinstance(value, dict)
                or set(value) != {"version", "device", "inode"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["device"]) is not int or value["device"] < 0
                or type(value["inode"]) is not int or value["inode"] < 0):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise ScreenError("RUNTIME_STATE_INVALID") from None


def assert_not_retired(directory: Path) -> None:
    if retirement(directory) is not None:
        raise ScreenError("PROFILE_IDENTITY_CHANGED")


def begin_retirement(profile: Path) -> None:
    """Called only after screen and agent quiescence, with both guards held."""
    if os.name != "posix":
        return
    from flowly.desktop_runtime.launcher import write_state

    info = profile.lstat()
    value = {"version": 1, "device": info.st_dev, "inode": info.st_ino}
    existing = retirement(profile / "desktop")
    if existing is not None and existing != value:
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    write_state(_prepare_retirement_path(profile / "desktop"), value)


def finish_retirement(profile: Path) -> None:
    # Absence proves that external data was removed first. Never clear the
    # durable fence merely because a destructive request lost its connection.
    if profile.exists() or profile.is_symlink():
        raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
    if os.name == "posix":
        path = _retirement_path(profile / "desktop")
        path.unlink(missing_ok=True)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def cleanup_screen_storage(runtime) -> None:
    """Idempotent cleanup after retirement, while lifecycle is still locked."""
    directory = runtime.directory
    if os.name != "posix":
        return
    if retirement(directory) is None:
        raise ScreenError("RUNTIME_STATE_INVALID")
    mapping = directory / "application-layout.json"
    if mapping.exists() or mapping.is_symlink():
        from flowly.desktop_runtime.application_layout import remove_root_application_storage

        remove_root_application_storage(directory)
    if os.name == "posix":
        parent = _guard_path(directory).parent
        key = hashlib.sha256(os.fsencode(directory)).hexdigest()[:24]
        for path in (parent / key, parent / (key + "-apps")):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or not shutil.rmtree.avoids_symlink_attacks:
                raise ScreenError("RUNTIME_STATE_INVALID")
            shutil.rmtree(path)


@contextmanager
def removable_screen(profile: Path):
    """Keep construction and start/stop fenced until the caller removes a profile.

    A failed or orphaned reservation is not proof of an idle browser. Deletion
    cannot substitute for explicit runtime recovery or silently release a human.
    """
    from flowly.desktop_runtime.launcher import ScreenRuntime, is_alive

    directory = profile / "desktop"
    with construction_guard(directory):
        try:
            profile_info = profile.lstat()
        except FileNotFoundError:
            profile_info = None
        if profile_info is not None and not stat.S_ISDIR(profile_info.st_mode):
            raise ScreenError("RUNTIME_STATE_INVALID")
        retiring = retirement(directory)
        if retiring is not None and profile_info is not None:
            if (profile_info.st_dev, profile_info.st_ino) != (retiring["device"], retiring["inode"]):
                raise ScreenError("PROFILE_IDENTITY_CHANGED")
        try:
            info = directory.lstat()
        except FileNotFoundError:
            yield None
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ScreenError("RUNTIME_STATE_INVALID")
        runtime = ScreenRuntime(directory, create=False)
        with runtime._lock:
            runtime.assert_current_directory()
            state = runtime.read_manifest()
            if state and (state["phase"] in {"starting", "running", "failed"}
                          or is_alive(state.get("supervisor"))
                          or any(is_alive(child) for child in state["children"])):
                raise ScreenError("RUNTIME_RECOVERY_REQUIRED" if state["phase"] == "failed" else "SCREEN_BUSY")
            control = runtime.authority.snapshot()
            if control["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if control["running"] or control["runId"] is not None or control["actionInFlight"] or control["inputDrainRequired"]:
                raise ScreenError("SCREEN_BUSY")
            yield runtime


def assert_screen_removable(profile: Path) -> None:
    with removable_screen(profile):
        pass
