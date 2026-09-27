"""Fixed account-provisioning source for the isolated privileged installer.

No name, UID, group, shell, home or filesystem path is accepted from a client.
An interrupted creation is retained for explicit recovery, never guessed/adopted.
"""

PROVISION_SOURCE = r'''
import fcntl
import grp
import json
import os
import pwd
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

ACCOUNT_ROOT = Path("/var/lib/flowly-screen-runtime")
ACCOUNT_NAME = "flowly-screen"
ACCOUNT_HOME = "/nonexistent"
ACCOUNT_SHELL = "/usr/sbin/nologin"

def account_directory(path, create=False):
    for component in reversed((path, *path.parents)):
        if create and component == path:
            component.mkdir(mode=0o755, exist_ok=True)
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("INSTALL_STATE_INVALID")

def account_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("INSTALL_STATE_INVALID")
        raw = source.read(2049)
    if len(raw) > 2048:
        raise ValueError("INSTALL_STATE_INVALID")
    return json.loads(raw)

def account_sync():
    fd = os.open(ACCOUNT_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def account_save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".account-", dir=ACCOUNT_ROOT)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fchmod(output.fileno(), 0o600)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        account_sync()
    finally:
        Path(temporary).unlink(missing_ok=True)

def account_identity(value=None):
    user, group = pwd.getpwnam(ACCOUNT_NAME), grp.getgrnam(ACCOUNT_NAME)
    actual = {"accountVersion": 1, "name": ACCOUNT_NAME, "uid": user.pw_uid,
              "gid": user.pw_gid, "home": user.pw_dir, "shell": user.pw_shell}
    if (not 0 < user.pw_uid < 2**31 or not 0 < user.pw_gid < 2**31
            or user.pw_dir != ACCOUNT_HOME or user.pw_shell != ACCOUNT_SHELL
            or group.gr_gid != user.pw_gid or group.gr_mem
            or pwd.getpwuid(user.pw_uid).pw_name != ACCOUNT_NAME
            or grp.getgrgid(user.pw_gid).gr_name != ACCOUNT_NAME
            or set(os.getgrouplist(ACCOUNT_NAME, user.pw_gid)) != {user.pw_gid}):
        raise ValueError("INSTALL_STATE_INVALID")
    if value is not None and (
            not isinstance(value, dict) or value != actual
            or type(value.get("accountVersion")) is not int
            or type(value.get("uid")) is not int or type(value.get("gid")) is not int):
        raise ValueError("INSTALL_STATE_INVALID")
    return actual

def provision_runtime_account():
    if os.geteuid() != 0:
        raise ValueError("INSTALL_PRIVILEGE_REQUIRED")
    account_directory(ACCOUNT_ROOT, create=True)
    lock = os.open(ACCOUNT_ROOT / "account.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("INSTALL_STATE_INVALID")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("INSTALL_BUSY") from None
        receipt = ACCOUNT_ROOT / "account.json"
        pending = ACCOUNT_ROOT / "account.pending"
        try:
            previous = account_read(receipt)
        except FileNotFoundError:
            previous = None
        else:
            # A JSON null receipt is corrupt provenance, not a missing receipt.
            if previous is None:
                raise ValueError("INSTALL_STATE_INVALID")
            return account_identity(previous)
        # Existence includes dangling links; never follow or clear a prior
        # incomplete reservation, even if useradd appears not to have run.
        try:
            pending.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(ACCOUNT_NAME)
            except KeyError:
                continue
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        account_save(pending, {"accountVersion": 1, "state": "creating"})
        # A timeout may leave a partially created system account. The durable
        # reservation prevents retries from adopting it or issuing useradd again.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        process = subprocess.Popen(
            ["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home",
             "--home-dir", ACCOUNT_HOME, "--shell", ACCOUNT_SHELL, "--password", "!", ACCOUNT_NAME],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            cwd="/", close_fds=True, start_new_session=True)
        try:
            code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            # Keep the unreaped leader until the group signal; no PID reuse.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            raise ValueError("INSTALL_RECOVERY_REQUIRED") from None
        if code:
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        value = account_identity()
        account_save(receipt, value)
        pending.unlink()
        account_sync()
        return value
    finally:
        os.close(lock)
'''

# Dedicated per-profile-incarnation accounts on root hosts. The name is always
# derived server-side from a random layout id; the fixed pattern is re-checked
# here, and the same reserve/receipt/recovery rules as the shared account apply.
PROFILE_ACCOUNT_SOURCE = PROVISION_SOURCE + r'''
import re
import shutil
import time

PROFILE_ACCOUNTS = ACCOUNT_ROOT / "accounts"
PROFILE_ACCOUNT = re.compile(r"flowly-s-[a-f0-9]{12}")
SHARED_TEMPORARY = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))

def profile_account_name(name):
    if not isinstance(name, str) or PROFILE_ACCOUNT.fullmatch(name) is None:
        raise ValueError("INVALID_PARAMS")
    return name

def profile_account_save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".account-", dir=PROFILE_ACCOUNTS)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fchmod(output.fileno(), 0o600)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    profile_account_sync()

def profile_account_sync():
    fd = os.open(PROFILE_ACCOUNTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def profile_account_identity(name, value=None):
    user, group = pwd.getpwnam(name), grp.getgrnam(name)
    actual = {"accountVersion": 1, "name": name, "uid": user.pw_uid,
              "gid": user.pw_gid, "home": user.pw_dir, "shell": user.pw_shell}
    if (not 0 < user.pw_uid < 2**31 or not 0 < user.pw_gid < 2**31
            or user.pw_dir != ACCOUNT_HOME or user.pw_shell != ACCOUNT_SHELL
            or group.gr_gid != user.pw_gid or group.gr_mem
            or pwd.getpwuid(user.pw_uid).pw_name != name
            or grp.getgrgid(user.pw_gid).gr_name != name
            or set(os.getgrouplist(name, user.pw_gid)) != {user.pw_gid}):
        raise ValueError("INSTALL_STATE_INVALID")
    if value is not None and (not isinstance(value, dict) or value != actual):
        raise ValueError("INSTALL_STATE_INVALID")
    return actual

def profile_account_locked(work):
    if os.geteuid() != 0:
        raise ValueError("INSTALL_PRIVILEGE_REQUIRED")
    # The installer-provisioned shared account is the provenance gate.
    account_directory(ACCOUNT_ROOT)
    account_identity(account_read(ACCOUNT_ROOT / "account.json"))
    account_directory(PROFILE_ACCOUNTS, create=True)
    lock = os.open(PROFILE_ACCOUNTS / "accounts.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("INSTALL_STATE_INVALID")
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError("INSTALL_BUSY") from None
                time.sleep(0.05)
        return work()
    finally:
        os.close(lock)

def account_run(command):
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                               cwd="/", close_fds=True, start_new_session=True)
    try:
        return process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        raise ValueError("INSTALL_RECOVERY_REQUIRED") from None

def provision_profile_account(name):
    name = profile_account_name(name)
    def work():
        receipt, pending = PROFILE_ACCOUNTS / (name + ".json"), PROFILE_ACCOUNTS / (name + ".pending")
        try:
            previous = account_read(receipt)
        except FileNotFoundError:
            pass
        else:
            if previous is None:
                raise ValueError("INSTALL_STATE_INVALID")
            return profile_account_identity(name, previous)
        try:
            pending.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(name)
            except KeyError:
                continue
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        profile_account_save(pending, {"accountVersion": 1, "state": "creating"})
        if account_run(["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home", "--home-dir",
                        ACCOUNT_HOME, "--shell", ACCOUNT_SHELL, "--password", "!", name]):
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        value = profile_account_identity(name)
        profile_account_save(receipt, value)
        pending.unlink()
        profile_account_sync()
        return value
    return profile_account_locked(work)

def owned_processes(uid):
    found = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        # Zombies (already killed, awaiting a parent that may never reap them,
        # e.g. a container's PID 1) hold no memory, files or code; skip them.
        state, owner = None, None
        try:
            with open(f"/proc/{entry.name}/status") as status:
                for line in status:
                    if line.startswith("State:"):
                        state = line.split()[1]
                    elif line.startswith("Uid:"):
                        owner = line.split()[1:]
                        break
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if owner is not None and str(uid) in owner and state not in {"Z", "X"}:
            found.append(int(entry.name))
    return found

def remove_owned_temporary(uid):
    # A retired UID may be reused by a later account; nothing it owned in
    # shared temporary storage may survive to be inherited.
    for base in SHARED_TEMPORARY:
        try:
            entries = list(os.scandir(base))
        except FileNotFoundError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if info.st_uid != uid:
                continue
            if stat.S_ISDIR(info.st_mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise ValueError("INSTALL_STATE_INVALID")
                shutil.rmtree(entry.path, ignore_errors=False)
            else:
                os.unlink(entry.path)

def retire_profile_account(name):
    name = profile_account_name(name)
    def work():
        receipt, pending = PROFILE_ACCOUNTS / (name + ".json"), PROFILE_ACCOUNTS / (name + ".pending")
        try:
            previous = account_read(receipt)
        except FileNotFoundError:
            try:
                pending.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError("INSTALL_RECOVERY_REQUIRED")
            for lookup in (pwd.getpwnam, grp.getgrnam):
                try:
                    lookup(name)
                except KeyError:
                    continue
                raise ValueError("INSTALL_RECOVERY_REQUIRED")  # never adopt an unreceipted account
            return None
        uid = profile_account_identity(name, previous)["uid"]
        # Every process of this account belongs to this profile's screen,
        # including daemonized descendants that closed inherited descriptors.
        deadline = time.monotonic() + 10
        while (pids := owned_processes(uid)):
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if time.monotonic() > deadline:
                raise ValueError("INSTALL_BUSY")
            time.sleep(0.05)
        remove_owned_temporary(uid)
        if account_run(["/usr/sbin/userdel", name]):
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(name)
            except KeyError:
                continue
            raise ValueError("INSTALL_RECOVERY_REQUIRED")
        receipt.unlink()
        profile_account_sync()
        return None
    return profile_account_locked(work)

def terminate_profile_account_processes(name):
    name = profile_account_name(name)
    def work():
        value = profile_account_identity(name, account_read(PROFILE_ACCOUNTS / (name + ".json")))
        deadline = time.monotonic() + 10
        while (pids := owned_processes(value["uid"])):
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if time.monotonic() > deadline:
                raise ValueError("INSTALL_BUSY")
            time.sleep(0.05)
        return None
    return profile_account_locked(work)
'''
