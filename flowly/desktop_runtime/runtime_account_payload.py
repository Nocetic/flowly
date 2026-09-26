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
