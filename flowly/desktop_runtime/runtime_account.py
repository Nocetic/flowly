"""Read-only verification of the account reserved for root-owned screen hosts.

Account presence alone is not provenance. Only the fixed installer may mint the
root-owned receipt; an unrelated pre-existing Unix account must never be adopted.
This module does not enable root screen launch or create/change Unix accounts.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError

ROOT = Path("/var/lib/flowly-screen-runtime")
ACCOUNT_NAME = "flowly-screen"
ACCOUNT_HOME = "/nonexistent"
ACCOUNT_SHELL = "/usr/sbin/nologin"


@dataclass(frozen=True)
class RuntimeAccount:
    uid: int
    gid: int


def _trusted_root(path: Path) -> None:
    # Include every ancestor: O_NOFOLLOW on the last component alone does not
    # protect a root process from a user replacing a writable parent directory.
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError()


def _account(value: object) -> RuntimeAccount:
    if (not isinstance(value, dict)
            or set(value) != {"accountVersion", "name", "uid", "gid", "home", "shell"}
            or type(value["accountVersion"]) is not int or value["accountVersion"] != 1
            or value["name"] != ACCOUNT_NAME or value["home"] != ACCOUNT_HOME
            or value["shell"] != ACCOUNT_SHELL
            or type(value["uid"]) is not int or not 0 < value["uid"] < 2**31
            or type(value["gid"]) is not int or not 0 < value["gid"] < 2**31):
        raise ValueError()
    # Import only on the supported Unix host; Desktop's Windows import graph
    # must not acquire a platform-only dependency.
    import grp
    import pwd

    user = pwd.getpwnam(ACCOUNT_NAME)
    group = grp.getgrnam(ACCOUNT_NAME)
    if (user.pw_uid != value["uid"] or user.pw_gid != value["gid"]
            or user.pw_dir != ACCOUNT_HOME or user.pw_shell != ACCOUNT_SHELL
            or group.gr_gid != value["gid"] or group.gr_mem
            or pwd.getpwuid(value["uid"]).pw_name != ACCOUNT_NAME
            or grp.getgrgid(value["gid"]).gr_name != ACCOUNT_NAME
            or set(os.getgrouplist(ACCOUNT_NAME, value["gid"])) != {value["gid"]}):
        raise ValueError()
    return RuntimeAccount(value["uid"], value["gid"])


def runtime_account() -> RuntimeAccount:
    """Fail closed for missing provenance, changed identity or extra groups."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ScreenError("UNPRIVILEGED_RUNTIME_REQUIRED")
    try:
        _trusted_root(ROOT)
        fd = os.open(ROOT / "account.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError()
            raw = source.read(2049)
        if len(raw) > 2048:
            raise ValueError()
        return _account(json.loads(raw))
    except FileNotFoundError:
        raise ScreenError("UNPRIVILEGED_RUNTIME_REQUIRED") from None
    except (OSError, ValueError, KeyError, UnicodeError):
        raise ScreenError("RUNTIME_STATE_INVALID") from None
