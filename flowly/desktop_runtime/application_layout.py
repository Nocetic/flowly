"""Root-owned mapping to unprivileged application storage outside /root.

The mapping and each application's directory entry remain agent-owned. Only leaf
directories belong to the runtime account. No recursive chown or caller path is
used. A new profile incarnation receives fresh storage even at a reused path.

Layout version 2 runs each profile incarnation under its own dedicated account
(see profile_accounts), so profiles cannot read each other's browser storage.
Version 1 layouts shared one account; they remain readable for status and stop
but are never started again (explicit recovery), never silently shared.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.profile_accounts import (
    account_name,
    profile_account,
    provision_profile_account,
    retire_profile_account,
)
from flowly.desktop_runtime.runtime_account import (
    ROOT,
    RuntimeAccount,
    _trusted_root,
    runtime_account,
)

SOCKET_ROOT = Path("/run/flowly-screen")
_LEAVES = ("home", "config", "cache", "data", "browser")


def _directory(path: Path, uid: int, gid: int, mode: int, *, create: bool) -> Path:
    made = False
    if create:
        try:
            # Parent is root-owned and not writable by the runtime account.
            path.mkdir(mode=0o700)
            made = True
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if made:
            if info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError()
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
            info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != mode):
            raise ValueError()
    finally:
        os.close(fd)
    return path


def _read_mapping(path: Path, digest: str) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError()
        raw = source.read(2049)
    if len(raw) > 2048:
        raise ValueError()
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get("layoutVersion")) is not int
            or value.get("source") != digest or not isinstance(value.get("id"), str)
            or re.fullmatch(r"[a-f0-9]{32}", value["id"]) is None):
        raise ValueError()
    if value["layoutVersion"] == 1:
        if (set(value) != {"layoutVersion", "id", "uid", "gid", "source"}
                or type(value["uid"]) is not int or type(value["gid"]) is not int):
            raise ValueError()
    elif value["layoutVersion"] == 2:
        if set(value) != {"layoutVersion", "id", "account", "source"} or value["account"] != account_name(value["id"]):
            raise ValueError()
    else:
        raise ValueError()
    return value


def _mapped_account(value: dict) -> RuntimeAccount:
    if value["layoutVersion"] == 2:
        return profile_account(value["account"])
    shared = runtime_account()
    if (value["uid"], value["gid"]) != (shared.uid, shared.gid):
        raise ValueError()
    return shared


@dataclass(frozen=True)
class RootApplicationLayout:
    account: RuntimeAccount
    directory: Path
    sockets: Path
    # Dedicated account of a version 2 layout; None for a legacy shared one.
    account_name: str | None = None

    @property
    def browser(self) -> Path:
        return self.directory / "browser"

    def environment(self) -> dict[str, str]:
        # No root agent environment or root-owned session bus reaches the apps.
        return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "HOME": str(self.directory / "home"), "XAUTHORITY": str(self.sockets / "Xauthority"),
                "XDG_RUNTIME_DIR": str(self.sockets), "XDG_CONFIG_HOME": str(self.directory / "config"),
                "XDG_CACHE_HOME": str(self.directory / "cache"), "XDG_DATA_HOME": str(self.directory / "data")}

    def read_browser_port(self) -> int | None:
        return read_browser_port(self.browser, self.account.uid)


def read_browser_port(browser: Path, uid: int) -> int | None:
    """Only a bounded regular application-owned port record may be read."""
    try:
        fd = os.open(browser / "DevToolsActivePort", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ScreenError("RUNTIME_STATE_INVALID") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or info.st_size > 128):
            raise ValueError()
        raw = os.read(fd, 129)
        if not raw:
            return None  # Chromium has created the file but not written it.
        if len(raw) > 128:
            raise ValueError()
        port = raw.splitlines()[0]
        if not re.fullmatch(rb"[0-9]{1,5}", port) or not 0 < int(port) < 65536:
            raise ValueError()
        return int(port)
    except (OSError, ValueError, IndexError):
        raise ScreenError("RUNTIME_STATE_INVALID") from None
    finally:
        os.close(fd)

def root_application_layout(directory: Path, *, create: bool = False) -> RootApplicationLayout:
    """Read-only by default; creation requires an already-owned lifecycle lock.

    A copied/moved mapping is rejected instead of silently sharing a browser.
    Migration and cleanup require the agent's explicit lifecycle coordination.
    """
    runtime_account()  # Installer provenance gate for every layout.
    directory = directory.absolute()
    try:
        _trusted_root(directory)
        info = directory.stat()
        if info.st_mode & 0o077:
            raise ValueError()
        mapping = directory / "application-layout.json"
        digest = hashlib.sha256(os.fsencode(directory)).hexdigest()
        if create:
            # Also serialize direct internal callers; read-only status never
            # creates a mapping, lock, account or application directory.
            with FileLock(directory / "application-layout.lock", timeout=5, mode=0o600):
                try:
                    value = _read_mapping(mapping, digest)
                except FileNotFoundError:
                    from flowly.desktop_runtime.launcher import write_state

                    layout_id = secrets.token_hex(16)
                    value = {"layoutVersion": 2, "id": layout_id, "account": account_name(layout_id), "source": digest}
                    # The mapping names the account before it exists, so an
                    # interrupted creation is resumed, never orphaned.
                    write_state(mapping, value)
                if value["layoutVersion"] != 2:
                    raise ScreenError("RUNTIME_RECOVERY_REQUIRED")
                account = provision_profile_account(value["account"])
        else:
            value = _read_mapping(mapping, digest)
            account = _mapped_account(value)
        _trusted_root(ROOT)
        _trusted_root(SOCKET_ROOT.parent)
        profiles = _directory(ROOT / "screens", 0, 0, 0o711, create=create)
        applications = _directory(profiles / value["id"], 0, 0, 0o711, create=create)
        for leaf in _LEAVES:
            _directory(applications / leaf, account.uid, account.gid, 0o700, create=create)
        sockets_parent = _directory(SOCKET_ROOT, 0, 0, 0o711, create=create)
        sockets = _directory(sockets_parent / value["id"], account.uid, account.gid, 0o700, create=create)
        return RootApplicationLayout(account, applications, sockets, value.get("account"))
    except (OSError, ValueError, KeyError, UnicodeError):
        raise ScreenError("RUNTIME_STATE_INVALID") from None


def remove_root_application_storage(directory: Path) -> None:
    """Remove only the mapped storage; allow retries after partial removal.

    The caller owns the screen lifecycle lock and a durable profile retirement.
    Unprivileged leaf contents are removed with fd-based, symlink-safe traversal.
    The immutable mapping is kept until the profile itself is removed.
    """
    runtime_account()
    try:
        _trusted_root(directory)
        digest = hashlib.sha256(os.fsencode(directory.absolute())).hexdigest()
        value = _read_mapping(directory / "application-layout.json", digest)
        try:
            account = _mapped_account(value)
        except ScreenError:
            if value["layoutVersion"] != 2:
                raise
            account = None  # Already retired; storage removal stays retryable.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError()
        sockets = ((SOCKET_ROOT, account.uid, account.gid, 0o700),) if account else ()
        for parent, uid, gid, mode in ((ROOT / "screens", 0, 0, 0o711), *sockets):
            _trusted_root(parent.parent)
            try:
                _directory(parent, 0, 0, 0o711, create=False)
            except FileNotFoundError:
                continue
            target = parent / value["id"]
            try:
                _directory(target, uid, gid, mode, create=False)
            except FileNotFoundError:
                continue
            shutil.rmtree(target)
        if value["layoutVersion"] == 2:
            # Kills any surviving process of this profile's account, clears its
            # temporary files and deletes the account; idempotent once gone.
            retire_profile_account(value["account"])
    except (OSError, ValueError, KeyError, UnicodeError):
        raise ScreenError("RUNTIME_STATE_INVALID") from None
