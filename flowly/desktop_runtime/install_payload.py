"""Fixed privileged package worker, executed by the host's isolated Python.

No package names, command fragments, paths or environment come from the caller.
Stdlib only: the agent's Python environment is never imported with sudo.
"""

from flowly.desktop_runtime.install_recipe import BROWSER_SOURCE, RECIPE_SOURCE
from flowly.desktop_runtime.runtime_account_payload import PROVISION_SOURCE

# Keep this executable standalone: the elevated interpreter must not import
# user-writable Core modules. The source itself is part of the reviewed plan;
# the recipe it installs is recomputed here and must match the reviewed one.
WORKER = PROVISION_SOURCE + RECIPE_SOURCE + BROWSER_SOURCE + r'''
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

ROOT = Path("/var/lib/flowly-screen-installer")
PROBE = os.path.exists
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
                or value["error"] not in (None, "INSTALL_RECOVERY_REQUIRED", "INSTALL_INDEX_FAILED", "INSTALL_PACKAGES_FAILED",
                                          "INSTALL_TIMEOUT", "INSTALL_HOST_PACKAGES_BROKEN", "INSTALL_ACCOUNT_CONFLICT",
                                          "INSTALL_DOWNLOAD_FAILED", "INSTALL_BROWSER_FAILED")
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
        # capture: how many bytes of output the caller needs (True reads one).
        return result, output.read(int(capture)) if capture else b""

PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+(:[a-z0-9-]+)?")
CONFIGURE = ["/usr/bin/dpkg", "--configure", "-a"]

def damaged_packages():
    """Packages dpkg left half-installed or marked reinstall-required, by name
    from dpkg's own database; None if that cannot be read safely."""
    code, raw = command(["/usr/bin/dpkg-query", "-W", "--showformat=${db:Status-Abbrev} ${binary:Package}\n"], 60,
                        capture=1 << 20)
    if code or len(raw) >= 1 << 20:
        return None
    names = []
    for line in raw.decode("utf-8", "replace").splitlines():
        flags, name = line[:3], line[3:].strip()
        if len(flags) == 3 and (flags[1] == "H" or flags[2] == "R"):
            if not PACKAGE_NAME.fullmatch(name):
                return None
            names.append(name)
    return names

def repair_packages():
    """Converge what an interrupted run of this installer left: finish pending
    configuration, reinstall half-installed packages (configuration cannot
    fix those; seen live when dpkg was stopped mid-unpack), then let apt fix
    dependencies. Never removes a package. True when every step succeeded."""
    # May fail on a half-installed package; the steps below and the final
    # audit decide.
    command(CONFIGURE, 300)
    damaged = damaged_packages()
    if damaged is None:
        return False
    if damaged:
        code, _ = command([*APT, "install", "--reinstall", "-y", "--no-install-recommends", "--no-remove", *damaged], 600)
        if code:
            return False
    code, _ = command(CONFIGURE, 300)
    if code:
        return False
    code, _ = command([*APT, "install", "-f", "-y", "--no-install-recommends", "--no-remove"], 600)
    return not code

def prepare_packages(repair):
    """None when the package system is ready for this installation, else the
    terminal (state, error) to record."""
    if repair and not repair_packages():
        return "recovery_required", "INSTALL_PACKAGES_FAILED"
    code, dirty = command(["/usr/bin/dpkg", "--audit"], 30, capture=True)
    if code or dirty:
        if repair:
            return "recovery_required", "INSTALL_PACKAGES_FAILED"
        # Unfinished package work this installer did not leave belongs to the
        # host's owner: change nothing and say what to run.
        return "failed", "INSTALL_HOST_PACKAGES_BROKEN"
    return None

DNF = ["/usr/bin/dnf", "-y", "--setopt=install_weak_deps=False"]

def rpm_duplicates():
    """Packages installed in two versions (an interrupted transaction leaves
    both); None if that cannot be read. dnf's own query: kernels and GPG keys
    are installed many times by design (a plain rpm -qa count flagged every
    EL9 host with two signing keys, seen live)."""
    code, raw = command(["/usr/bin/dnf", "-q", "repoquery", "--duplicates", "--qf", "%{name}.%{arch}\\n"], 300,
                        capture=1 << 22)
    if code or len(raw) >= 1 << 22:
        return None
    return sorted({line.strip() for line in raw.decode("utf-8", "replace").splitlines() if line.strip()})

def prepare_rpm(repair):
    if repair:
        # Keep the newer of two half-replaced versions; never anything else.
        command([*DNF, "remove", "--duplicates"], 600)
    duplicates = rpm_duplicates()
    if duplicates is None or duplicates:
        return ("recovery_required", "INSTALL_PACKAGES_FAILED") if repair else ("failed", "INSTALL_HOST_PACKAGES_BROKEN")
    return None

def prepare(recipe, repair):
    return prepare_packages(repair) if recipe["family"] == "apt" else prepare_rpm(repair)

def refresh(recipe):
    """Refresh package indexes (and enable the recipe's repositories)."""
    if recipe["family"] == "apt":
        return command([*APT, "-o", "APT::Update::Error-Mode=any", "update"], 180)[0]
    if "epel" in recipe["repositories"]:
        code, _ = command([*DNF, "install", "epel-release", "dnf-plugins-core"], 300)
        if code:
            return code
    if recipe["repositories"]:
        # Enabled for good, as the plan says: an installed but disabled EPEL
        # (seen live) would leave Chromium without its security updates.
        code, _ = command(["/usr/bin/dnf", "config-manager", "--set-enabled", *recipe["repositories"]], 60)
        if code:
            return code
    return command([*DNF, "makecache"], 300)[0]

def install_packages(recipe):
    if recipe["family"] == "apt":
        return command([*APT, "install", "-y", "--no-install-recommends", "--no-remove", "--no-upgrade",
                        *recipe["packages"]], 600)[0]
    return command([*DNF, "install", *recipe["packages"]], 900)[0]

def main():
    if os.geteuid() != 0 or len(sys.argv) != 4:
        return fail("INSTALL_PRIVILEGE_REQUIRED")
    # Gateway/service parents may ignore SIGCHLD. Package exit codes and the
    # unreaped-leader timeout fence require ordinary wait/reap semantics.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    operation, plan, reviewed = sys.argv[1:]
    if (not re.fullmatch(r"[a-f0-9]{32}", operation) or not re.fullmatch(r"[a-f0-9]{64}", plan)
            or not re.fullmatch(r"[a-f0-9]{64}", reviewed)):
        return fail("INVALID_PARAMS")
    # This host's recipe, from this host alone, must be the one reviewed.
    recipe = screen_recipe(os_release(), platform.machine(), PROBE)
    if recipe is None or recipe_id(recipe) != reviewed:
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
        # Under the host lock a "running" receipt was interrupted. Work this
        # installer left uncertain since its last success is repaired by this
        # new operation; the interrupted operation itself is never replayed.
        history = [read(receipt) for receipt in receipts]
        succeeded = max((item["updatedAt"] for item in history if item["state"] == "succeeded"), default=-1)
        repair = any(item["state"] in ("running", "recovery_required") and item["updatedAt"] >= succeeded
                     for item in history)
        value = {"installVersion": 1, "operationId": operation, "planId": plan,
                 "state": "running", "phase": "checking", "error": None,
                 "updatedAt": int(time.time())}
        save(path, value)
        try:
            problem = prepare(recipe, repair)
            if problem:
                value.update(state=problem[0], error=problem[1])
            else:
                value.update(phase="updating", updatedAt=int(time.time()))
                save(path, value)
                if refresh(recipe):
                    value.update(state="failed", error="INSTALL_INDEX_FAILED")
                else:
                    value.update(phase="installing", updatedAt=int(time.time()))
                    save(path, value)
                    if install_packages(recipe):
                        value.update(state="recovery_required", error="INSTALL_PACKAGES_FAILED")
                    else:
                        # Root-hosted agents need a separate locked application
                        # account. This fixed step is part of the reviewed worker
                        # fingerprint and runs before its success receipt.
                        try:
                            if recipe["browser"]["source"] == "pinned":
                                install_pinned_browser(recipe["architecture"])
                            elif recipe["browser"]["package"] == GOOGLE_CHROME["package"]:
                                add_google_chrome(APT)
                            if recipe["apparmor"]:
                                install_apparmor_profile()
                            if uid == "0":
                                provision_runtime_account()
                        except BrowserInstallError as exc:
                            # Verified or nothing: no half-published browser.
                            value.update(state="failed", error=exc.code)
                        except ValueError as exc:
                            # Someone else's account under our name: nothing of
                            # ours is half-done, and it is not ours to change.
                            if str(exc) != "INSTALL_ACCOUNT_CONFLICT":
                                raise
                            value.update(state="failed", error="INSTALL_ACCOUNT_CONFLICT")
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
