"""Explicit, fixed-plan host package installation; never a shell/RPC executor.

The privileged worker owns a host-wide lock and durable, secret-free receipts.
Python extras and unprivileged runtime provisioning are separate plan steps.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.install_payload import WORKER
from flowly.desktop_runtime.install_recipe import os_release, recipe_id, screen_recipe

ROOT = Path("/var/lib/flowly-screen-installer")
_ERRORS = {"INSTALL_BUSY", "INSTALL_STATE_INVALID", "INSTALL_PRIVILEGE_REQUIRED", "INSTALL_PLAN_CHANGED",
           "INSTALL_RECOVERY_REQUIRED", "INSTALL_HISTORY_FULL", "OPERATION_CONFLICT", "INVALID_PARAMS",
           "INSTALL_INDEX_FAILED", "INSTALL_PACKAGES_FAILED", "INSTALL_TIMEOUT", "INSTALL_HOST_PACKAGES_BROKEN",
           "INSTALL_ACCOUNT_CONFLICT", "INSTALL_DOWNLOAD_FAILED", "INSTALL_BROWSER_FAILED"}
# Package index, packages, the pinned browser's download and a repair, bounded
# by each command's own timeout; jobs and clients wait longer than this.
WORKER_SECONDS = 1800
_exists = os.path.exists  # the recipe's fixed path probes


def _sudo_available() -> bool:
    return Path("/usr/bin/sudo").is_file() and os.access("/usr/bin/sudo", os.X_OK)


def install_plan() -> dict:
    """No subprocess, sudo probe, directory creation or package-manager write."""
    architecture = platform.machine()
    recipe = screen_recipe(os_release(), architecture, _exists) if sys.platform == "linux" else None
    supported = recipe is not None
    tools = supported and Path("/usr/bin/python3").is_file() and os.access("/usr/bin/python3", os.X_OK)
    reason = "INSTALL_UNSUPPORTED_HOST" if not supported else "INSTALL_TOOLS_MISSING" if not tools else None
    body = {"installVersion": 1, "distribution": recipe["distribution"] if supported else None,
            "architecture": architecture if architecture in {"x86_64", "aarch64"} else None,
            "packages": list(recipe["packages"]) if supported else [],
            "browser": dict(recipe["browser"]) if supported else None,
            "repositories": list(recipe["repositories"]) if supported else [],
            "recipeId": recipe_id(recipe) if supported else None,
            "supported": reason is None, "reason": reason, "scope": "system-packages", "requiresRoot": True,
            "pythonExtra": "screen", "runtimeAccountRequired": bool(hasattr(os, "geteuid") and os.geteuid() == 0),
            "sudoAvailable": bool(supported and _sudo_available()), "maximumSeconds": WORKER_SECONDS}
    # Changes to any executable part require a freshly reviewed plan. No paths
    # or credentials are in this stable public fingerprint.
    digest = hashlib.sha256((json.dumps(body, sort_keys=True) + WORKER).encode()).hexdigest()
    return {**body, "planId": digest}


def _operation(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ScreenError("INVALID_PARAMS")
    return value


def _receipt(value: object, operation: str, plan: str | None = None) -> dict:
    keys = {"installVersion", "operationId", "planId", "state", "phase", "error", "updatedAt"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["installVersion"]) is not int or value["installVersion"] != 1
            or value["operationId"] != operation or not isinstance(value["planId"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["planId"])
            or (plan is not None and value["planId"] != plan)
            or not isinstance(value["state"], str) or value["state"] not in {"running", "succeeded", "failed", "recovery_required"}
            or not isinstance(value["phase"], str) or value["phase"] not in {"checking", "updating", "installing", "complete"}
            or (value["error"] is not None and (not isinstance(value["error"], str) or value["error"] not in _ERRORS))
            or ((value["state"] in {"failed", "recovery_required"}) != (value["error"] is not None))
            or (value["state"] == "succeeded") != (value["phase"] == "complete")
            or type(value["updatedAt"]) is not int or not 0 <= value["updatedAt"] < 2**53):
        raise ScreenError("INSTALL_STATE_INVALID")
    return dict(value)


def install_status(operation: str) -> dict:
    """Read a root-owned public receipt; never ask sudo for status."""
    _operation(operation)
    if sys.platform != "linux":
        raise ScreenError("INSTALL_UNSUPPORTED_HOST")
    try:
        info = ROOT.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError()
        path = ROOT / f"{os.getuid()}-{operation}.json"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError()
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        value = _receipt(json.loads(raw), operation)
        if value["state"] == "running":
            import fcntl
            lock = os.open(ROOT / "packages.lock", os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError()
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    # A running receipt without the host lock is an interrupted
                    # worker, not permission to replay package operations.
                    value.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
            finally:
                os.close(lock)
        return value
    except FileNotFoundError:
        raise ScreenError("OPERATION_NOT_FOUND") from None
    except (OSError, ValueError):
        raise ScreenError("INSTALL_STATE_INVALID") from None


def install_system(plan_id: str, operation: str, *, password: str | None = None) -> dict:
    """Blocking CLI entry; worker receipts survive caller/gateway disconnects.

    A future asynchronous gateway admission can call the same fixed worker and
    poll its receipt. No client may supply package names, commands or paths.
    """
    _operation(operation)
    plan = install_plan()
    if not plan["supported"]:
        raise ScreenError(plan["reason"])
    if plan_id != plan["planId"]:
        raise ScreenError("INSTALL_PLAN_CHANGED")
    if password is not None:
        if not isinstance(password, str) or len(password) > 4096 or any(char in password for char in "\r\n\x00"):
            raise ScreenError("INVALID_PARAMS")
        try:
            if len(password.encode()) > 4096:
                raise ScreenError("INVALID_PARAMS")
        except UnicodeError:
            raise ScreenError("INVALID_PARAMS") from None
    command = ["/usr/bin/python3", "-I", "-c", WORKER, operation, plan_id, plan["recipeId"]]
    if os.geteuid() != 0:
        if not _sudo_available():
            raise ScreenError("INSTALL_PRIVILEGE_REQUIRED")
        command = ["/usr/bin/sudo", *(["-S", "-p", ""] if password is not None else ["-n"]), "--", *command]
    # No inherited proxy/auth/agent environment reaches the privileged worker.
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    child = None
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=env, cwd="/", start_new_session=True, close_fds=True)
        output, _ = child.communicate(input=(password + "\n").encode() if password is not None else b"", timeout=WORKER_SECONDS)
    except subprocess.TimeoutExpired:
        # Do not launch again or assume apt was cancelled: the privileged worker
        # has its own package timeouts and may still publish a terminal receipt.
        raise ScreenError("INSTALL_TIMEOUT") from None
    except OSError:
        raise ScreenError("INSTALL_PRIVILEGE_REQUIRED") from None
    finally:
        password = None
        if child is not None:
            # Never use Popen's context manager here: its __exit__ waits without
            # a deadline, undoing the bounded observation timeout above.
            for stream in (child.stdin, child.stdout):
                if stream:
                    stream.close()
            child.poll()
    if len(output) > 4096:
        raise ScreenError("INSTALL_STATE_INVALID")
    try:
        value = json.loads(output)
    except (ValueError, UnicodeError):
        raise ScreenError("INSTALL_PRIVILEGE_REQUIRED") from None
    if isinstance(value, dict) and set(value) == {"error"}:
        code = value["error"]
        raise ScreenError(code if isinstance(code, str) and code in _ERRORS else "INSTALL_STATE_INVALID")
    return _receipt(value, operation, plan_id)
