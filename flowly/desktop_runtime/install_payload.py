"""Fixed privileged package worker, executed by the host's isolated Python.

No package names, command fragments, paths or environment come from the caller.
Stdlib only: the agent's Python environment is never imported with sudo.
"""

PACKAGES = (
    "tigervnc-standalone-server", "xauth", "openbox", "dbus-x11",
    "chromium", "chromium-sandbox", "xdotool", "x11-utils", "fonts-dejavu-core",
)

# Keep this executable standalone: the elevated interpreter must not import
# user-writable Core modules. The source itself is part of the reviewed plan.
WORKER = r'''
import fcntl
import json
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PACKAGES = ("tigervnc-standalone-server", "xauth", "openbox", "dbus-x11",
            "chromium", "chromium-sandbox", "xdotool", "x11-utils", "fonts-dejavu-core")
ROOT = Path("/var/lib/flowly-screen-installer")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
       "LC_ALL": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
APT = ["/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=20", "-o", "Acquire::Retries=1",
       "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30",
       "-o", "Dpkg::Options::=--force-confold"]

def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

def fail(code):
    emit({"error": code})
    return 1

def directory():
    ROOT.mkdir(mode=0o755, exist_ok=True)
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError()
    ROOT.chmod(0o755)

def save(path, value):
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, separators=(",", ":"))
            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
        os.replace(name, path)
        root_fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass

def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError()
        text = source.read(4097)
        if len(text) > 4096:
            raise ValueError()
        value = json.loads(text)
        if (not isinstance(value, dict) or set(value) != {"installVersion", "operationId", "planId", "state", "phase", "error", "updatedAt"}
                or type(value["installVersion"]) is not int or value["installVersion"] != 1
                or not isinstance(value["operationId"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["operationId"])
                or not isinstance(value["planId"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["planId"])
                or value["state"] not in ("running", "succeeded", "failed", "recovery_required")
                or value["phase"] not in ("checking", "updating", "installing", "complete")
                or value["error"] not in (None, "INSTALL_RECOVERY_REQUIRED", "INSTALL_INDEX_FAILED", "INSTALL_PACKAGES_FAILED", "INSTALL_TIMEOUT")
                or (value["state"] in ("failed", "recovery_required")) != (value["error"] is not None)
                or (value["state"] == "succeeded") != (value["phase"] == "complete")
                or type(value["updatedAt"]) is not int or not 0 <= value["updatedAt"] < 2**53):
            raise ValueError()
        return value

def command(args, timeout, capture=False):
    # A process group belongs only to this child and its descendants. Root
    # package tools, including maintainer scripts, stay under the timeout.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
            stdout=output if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=ENV, cwd="/", start_new_session=True, close_fds=True)
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Keep the group leader unreaped until the last signal: its PID
            # cannot be reused for an unrelated process group during cleanup.
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
        output.seek(0)
        return result, output.read(1) if capture else b""

def main():
    if os.geteuid() != 0 or len(sys.argv) != 4:
        return fail("INSTALL_PRIVILEGE_REQUIRED")
    # Gateway/service parents may ignore SIGCHLD. Package exit codes and the
    # unreaped-leader timeout fence require ordinary wait/reap semantics.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    operation, plan, architecture = sys.argv[1:]
    if not re.fullmatch(r"[a-f0-9]{32}", operation) or not re.fullmatch(r"[a-f0-9]{64}", plan):
        return fail("INVALID_PARAMS")
    release = platform.freedesktop_os_release()
    if release.get("ID") != "debian" or release.get("VERSION_ID") != "12" or platform.machine() != architecture:
        return fail("INSTALL_PLAN_CHANGED")
    uid = os.environ.get("SUDO_UID", "0")
    if not re.fullmatch(r"[0-9]{1,10}", uid):
        return fail("INSTALL_STATE_INVALID")
    directory()
    fd = os.open(ROOT / "packages.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        os.close(fd)
        return fail("INSTALL_STATE_INVALID")
    os.fchmod(fd, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return fail("INSTALL_BUSY")
        path = ROOT / (uid + "-" + operation + ".json")
        try:
            previous = read(path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous.get("planId") != plan:
                return fail("OPERATION_CONFLICT")
            if previous.get("state") == "running":
                previous.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
                save(path, previous)
            emit(previous)
            return 0 if previous.get("state") == "succeeded" else 1
        # Never silently discard a receipt: a forgotten operation could replay
        # after a disconnect. Maintenance/recovery is explicit, not pruning.
        receipts = list(ROOT.glob("*.json"))
        if len(receipts) >= 128:
            return fail("INSTALL_HISTORY_FULL")
        for receipt in receipts:
            if read(receipt).get("state") in {"running", "recovery_required"}:
                return fail("INSTALL_RECOVERY_REQUIRED")
        value = {"installVersion": 1, "operationId": operation, "planId": plan,
                 "state": "running", "phase": "checking", "error": None,
                 "updatedAt": int(time.time())}
        save(path, value)
        try:
            code, dirty = command(["/usr/bin/dpkg", "--audit"], 30, capture=True)
            if code or dirty:
                value.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
            else:
                value.update(phase="updating", updatedAt=int(time.time()))
                save(path, value)
                code, _ = command([*APT, "-o", "APT::Update::Error-Mode=any", "update"], 180)
                if code:
                    value.update(state="failed", error="INSTALL_INDEX_FAILED")
                else:
                    value.update(phase="installing", updatedAt=int(time.time()))
                    save(path, value)
                    code, _ = command([*APT, "install", "-y", "--no-install-recommends", "--no-remove", "--no-upgrade", *PACKAGES], 600)
                    if code:
                        value.update(state="recovery_required", error="INSTALL_PACKAGES_FAILED")
                    else:
                        value.update(state="succeeded", phase="complete")
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

try:
    sys.exit(main())
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(fail("INSTALL_STATE_INVALID"))
'''
