"""Standalone stdlib worker for the current agent virtualenv (never sudo).

Executed as a file with Python -I so installing optional packages cannot replace
imports used by the worker. Arguments are constructed only by python_installer.
"""

import hashlib
import importlib.metadata
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSIONS = {"greenlet": "3.5.6", "playwright": "1.58.0", "pyee": "13.0.1", "typing-extensions": "4.15.0"}
# Only the screen uses these: installed (or upgraded) to their pins.
OWN = ("playwright", "pyee")
# The agent may already use these. Never replaced (seen live: a pinned
# typing-extensions replaced a newer one and anyio could not import, so the
# gateway never started again). Installed at the pin only when absent; the
# range is what playwright and pyee declare. Lower bound inclusive, upper
# bound exclusive (None: open).
SHARED = {"greenlet": ((3, 1, 1), (4,)), "typing-extensions": ((0,), None)}
ERRORS = {"INSTALL_BUSY", "INSTALL_STATE_INVALID", "INSTALL_PLAN_CHANGED", "INSTALL_RECOVERY_REQUIRED",
          "INSTALL_HISTORY_FULL", "OPERATION_CONFLICT", "INSTALL_PYTHON_FAILED", "INSTALL_TIMEOUT",
          "INSTALL_VERIFICATION_FAILED", "INSTALL_VIRTUALENV_REQUIRED", "INVALID_PARAMS",
          "INSTALL_DEPENDENCY_CONFLICT"}


def installed(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _release(version):
    parts = []
    for piece in re.split(r"[.+-]", version):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def compatible(name, version):
    lower, upper = SHARED[name]
    release = _release(version)
    return release >= lower and (upper is None or release < upper)


def conflicts():
    """Shared packages the agent has at a version the screen cannot use."""
    return [name for name in SHARED if installed(name) is not None and not compatible(name, installed(name))]


def needed(repair=False):
    """What to install: the screen's own packages unless already at their pins
    (always, to repair), and a shared package only where the agent has none."""
    return ([name for name in OWN if repair or installed(name) != VERSIONS[name]]
            + [name for name in SHARED if installed(name) is None])


def ready():
    return (all(installed(name) == VERSIONS[name] for name in OWN)
            and all(installed(name) is not None and compatible(name, installed(name)) for name in SHARED))


def requirements_for(names, source):
    """The hash-pinned blocks of screen-requirements.txt for these packages only."""
    blocks, current = {}, None
    for line in source.splitlines():
        match = re.match(r"([A-Za-z0-9_.-]+)==", line)
        if match:
            current = match.group(1).lower()
            blocks[current] = []
        if current is not None and (match or line.startswith(" ")):
            blocks[current].append(line)
        elif not match:
            current = None
    return "".join("\n".join(blocks[name]) + "\n" for name in names)


def state_root():
    return Path(sys.prefix).resolve() / ".flowly-screen-install"


def validate(value, operation=None):
    keys = {"installVersion", "operationId", "planId", "state", "phase", "error", "updatedAt", "restartRequired"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["installVersion"]) is not int or value["installVersion"] != 1
            or not isinstance(value["operationId"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["operationId"])
            or (operation is not None and value["operationId"] != operation)
            or not isinstance(value["planId"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["planId"])
            or value["state"] not in ("running", "succeeded", "failed", "recovery_required")
            or value["phase"] not in ("installing", "verifying", "complete")
            or value["error"] not in (None, *ERRORS)
            or (value["state"] in ("failed", "recovery_required")) != (value["error"] is not None)
            or (value["state"] == "succeeded") != (value["phase"] == "complete")
            or type(value["restartRequired"]) is not bool
            or type(value["updatedAt"]) is not int or not 0 <= value["updatedAt"] < 2**53):
        raise ValueError()
    return dict(value)


def private_directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError()


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError()
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError()
    return validate(json.loads(raw), path.stem)


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(validate(value, path.stem), output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def fail(code):
    emit({"error": code})
    return 1


def run_command(command, timeout):
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                   "UV_HTTP_TIMEOUT": "30", "UV_HTTP_RETRIES": "1"}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment, cwd="/", close_fds=True, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            time.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        raise TimeoutError() from None


def install(uv, requirements, reinstall=False):
    return run_command([
        uv, "--no-config", "--no-cache", "--no-progress", "--no-python-downloads", "pip", "install",
        "--python", sys.executable, "--index-url", "https://pypi.org/simple", "--require-hashes",
        "--only-binary", ":all:", "--no-deps", "--requirements", str(requirements),
        *(["--reinstall"] if reinstall else []),
    ], 300)


def verify():
    # Fresh imports and the actual bundled driver prove more than dist-info.
    # Starting Playwright's driver does not download or launch a browser.
    return run_command([sys.executable, "-I", "-c",
                        "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.stop()"], 15) == 0


def main():
    if sys.platform != "linux" or sys.prefix == sys.base_prefix:
        return fail("INSTALL_VIRTUALENV_REQUIRED")
    if len(sys.argv) != 5:
        return fail("INVALID_PARAMS")
    operation, plan, uv, digest = sys.argv[1:]
    if (not re.fullmatch(r"[a-f0-9]{32}", operation) or not re.fullmatch(r"[a-f0-9]{64}", plan)
            or not Path(uv).is_absolute()):
        return fail("INVALID_PARAMS")
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    requirements = Path(__file__).with_name("screen-requirements.txt")
    raw = requirements.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != digest:
        return fail("INSTALL_PLAN_CHANGED")
    if conflicts():
        return fail("INSTALL_DEPENDENCY_CONFLICT")
    root = state_root()
    private_directory(root, create=True)
    import fcntl
    fd = os.open(root / "python.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return fail("INSTALL_BUSY")
        path = root / (operation + ".json")
        try:
            previous = read(path)
        except FileNotFoundError:
            previous = None
        if previous:
            if previous["planId"] != plan:
                return fail("OPERATION_CONFLICT")
            if previous["state"] == "running":
                previous.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
                save(path, previous)
            emit(previous)
            return 0 if previous["state"] == "succeeded" else 1
        receipts = list(root.glob("*.json"))
        if len(receipts) >= 128:
            return fail("INSTALL_HISTORY_FULL")
        # Under the venv lock a "running" receipt was interrupted. Work left
        # uncertain since the last success is repaired by reinstalling the same
        # hash-pinned set; the interrupted operation itself is never replayed.
        history = [read(receipt) for receipt in receipts]
        succeeded = max((item["updatedAt"] for item in history if item["state"] == "succeeded"), default=-1)
        repair = any(item["state"] in ("running", "recovery_required") and item["updatedAt"] >= succeeded
                     for item in history)
        value = {"installVersion": 1, "operationId": operation, "planId": plan, "state": "running",
                 "phase": "installing", "error": None, "updatedAt": int(time.time()), "restartRequired": False}
        save(path, value)
        try:
            names = needed(repair)
            if not names:
                code = 0
            else:
                # Packages change under the running agent: it must restart.
                value["restartRequired"] = True
                save(path, value)
                subset = root / ("requirements-" + operation + ".txt")
                subset.write_text(requirements_for(names, raw.decode()))
                subset.chmod(0o600)
                try:
                    code = install(uv, subset, reinstall=True) if repair else install(uv, subset)
                finally:
                    subset.unlink(missing_ok=True)
            if code:
                value.update(state="recovery_required", error="INSTALL_PYTHON_FAILED")
            else:
                value.update(phase="verifying", updatedAt=int(time.time()))
                save(path, value)
                if ready() and verify():
                    value.update(state="succeeded", phase="complete")
                else:
                    value.update(state="recovery_required", error="INSTALL_VERIFICATION_FAILED")
        except TimeoutError:
            value.update(state="recovery_required", error="INSTALL_TIMEOUT")
        except BaseException:
            value.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
        value["updatedAt"] = int(time.time())
        save(path, value)
        emit(value)
        return 0 if value["state"] == "succeeded" else 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError):
        sys.exit(fail("INSTALL_STATE_INVALID"))
