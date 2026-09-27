"""Hash-locked optional screen dependencies in the existing agent virtualenv."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from flowly.desktop_runtime import python_install_worker as worker
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.installer import _operation


def find_uv() -> str | None:
    """uv on PATH, else where Flowly's installer and uv's own installer put it.

    A systemd user service's PATH omits ~/.local/bin, so a normally installed
    gateway would otherwise never see the uv that installed it. Fallbacks are
    trusted only as regular executables owned by this user or root and not
    writable by group or others.
    """
    found = shutil.which("uv")
    if found and Path(found).is_absolute():
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv"):
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.stat()
        except (OSError, RuntimeError):
            continue
        if (stat.S_ISREG(info.st_mode) and info.st_uid in {os.geteuid(), 0}
                and not info.st_mode & 0o022 and os.access(resolved, os.X_OK)):
            return str(resolved)
    return None


def _recipe() -> tuple[Path, bytes, str]:
    source = Path(worker.__file__)
    requirements = source.with_name("screen-requirements.txt").read_bytes()
    if len(requirements) > 65536:
        raise ScreenError("INSTALL_STATE_INVALID")
    return source, requirements, hashlib.sha256(requirements).hexdigest()


def python_plan() -> dict:
    """Read-only preview, stable across installation but bound to this env."""
    reason = None
    uv = find_uv()
    if sys.platform != "linux" or platform.machine() not in {"aarch64", "x86_64"}:
        reason = "INSTALL_UNSUPPORTED_HOST"
    elif getattr(sys, "frozen", False) or "__compiled__" in globals():
        reason = "INSTALL_MANAGED_BUILD_REQUIRED"
    elif sys.prefix == sys.base_prefix or not (Path(sys.prefix) / "pyvenv.cfg").is_file():
        reason = "INSTALL_VIRTUALENV_REQUIRED"
    elif not uv:
        reason = "INSTALL_UV_REQUIRED"
    try:
        source, requirements, digest = _recipe()
        implementation = hashlib.sha256(source.read_bytes()).hexdigest()
        up_to_date = worker.ready()
    except (OSError, ValueError, TypeError, AttributeError):
        digest, implementation, up_to_date = "", "", False
        reason = reason or "INSTALL_STATE_INVALID"
    body = {"installVersion": 1, "scope": "agent-python", "supported": reason is None, "reason": reason,
            "requiresRoot": False, "maximumSeconds": 330,
            "packages": [{"name": name, "version": version} for name, version in worker.VERSIONS.items()],
            "restartAfterChanges": True, "index": "https://pypi.org/simple"}
    identity = [str(Path(sys.prefix).resolve()), os.path.abspath(sys.executable), sys.version,
                platform.machine(), uv, digest, implementation]
    plan_id = hashlib.sha256(json.dumps([body, identity], sort_keys=True).encode()).hexdigest()
    return {**body, "planId": plan_id, "upToDate": up_to_date}


def python_status(operation: str) -> dict:
    _operation(operation)
    if sys.platform != "linux" or sys.prefix == sys.base_prefix:
        raise ScreenError("INSTALL_VIRTUALENV_REQUIRED")
    try:
        import fcntl
        root = worker.state_root()
        worker.private_directory(root)
        value = worker.read(root / (operation + ".json"))
        if value["state"] == "running":
            fd = os.open(root / "python.lock", os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError()
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    value.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
            finally:
                os.close(fd)
        return value
    except FileNotFoundError:
        raise ScreenError("OPERATION_NOT_FOUND") from None
    except (OSError, ValueError, TypeError):
        raise ScreenError("INSTALL_STATE_INVALID") from None


def install_python(plan_id: str, operation: str) -> dict:
    _operation(operation)
    plan = python_plan()
    if not plan["supported"]:
        raise ScreenError(plan["reason"])
    if plan_id != plan["planId"]:
        raise ScreenError("INSTALL_PLAN_CHANGED")
    try:
        source, _, digest = _recipe()
        uv = find_uv()
        if not uv:
            raise ScreenError("INSTALL_UV_REQUIRED")
        command = [sys.executable, "-I", str(source), operation, plan_id, uv, digest]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                                 cwd="/", start_new_session=True, close_fds=True)
        try:
            output, _ = child.communicate(timeout=330)
        finally:
            child.stdout.close()
            child.poll()
        if len(output) > 4096:
            raise ValueError()
        value = json.loads(output)
        if isinstance(value, dict) and set(value) == {"error"}:
            code = value["error"]
            raise ScreenError(code if isinstance(code, str) and code in worker.ERRORS else "INSTALL_STATE_INVALID")
        receipt = worker.validate(value, operation)
        if receipt["planId"] != plan_id:
            raise ValueError()
        return receipt
    except subprocess.TimeoutExpired:
        raise ScreenError("INSTALL_TIMEOUT") from None
    except (OSError, ValueError, TypeError):
        raise ScreenError("INSTALL_STATE_INVALID") from None
