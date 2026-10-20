"""Root runtime account provenance and changed-account rejection."""

import json
import os
import stat
from types import SimpleNamespace

import pytest

from flowly.desktop_runtime import runtime_account as account
from flowly.desktop_runtime.authority import ScreenError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix account verifier")


@pytest.fixture
def identities(monkeypatch):
    import grp
    import pwd

    user = SimpleNamespace(pw_name="flowly-screen", pw_uid=991, pw_gid=992,
                           pw_dir="/nonexistent", pw_shell="/usr/sbin/nologin")
    group = SimpleNamespace(gr_name="flowly-screen", gr_gid=992, gr_mem=[])
    monkeypatch.setattr(pwd, "getpwnam", lambda name: user)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: user)
    monkeypatch.setattr(grp, "getgrnam", lambda name: group)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: group)
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [gid])
    value = dict(accountVersion=1, name="flowly-screen", uid=991, gid=992,
                 home="/nonexistent", shell="/usr/sbin/nologin")
    return value, user, group


def test_exact_account_is_immutable(identities):
    from dataclasses import FrozenInstanceError

    result = account._account(identities[0])
    assert (result.uid, result.gid) == (991, 992)
    with pytest.raises(FrozenInstanceError):
        result.uid = 0


@pytest.mark.parametrize("key,value", [
    ("accountVersion", True), ("accountVersion", 2), ("name", "root"),
    ("uid", 0), ("uid", True), ("uid", "991"), ("uid", 2**31),
    ("gid", 0), ("gid", -1), ("gid", False), ("gid", 2**31),
    ("home", "/root"), ("shell", "/bin/sh"), ("extra", "ignored"),
])
def test_receipt_cannot_choose_arbitrary_account(identities, key, value):
    with pytest.raises(ValueError):
        account._account({**identities[0], key: value})


@pytest.mark.parametrize("which,key,value", [
    (1, "pw_name", "reused-uid"), (1, "pw_uid", 993), (1, "pw_gid", 993),
    (1, "pw_dir", "/root"), (1, "pw_shell", "/bin/bash"),
    (2, "gr_name", "reused-gid"), (2, "gr_gid", 993), (2, "gr_mem", ["someone"]),
])
def test_os_identity_changes_are_not_adopted(identities, which, key, value):
    setattr(identities[which], key, value)
    with pytest.raises(ValueError):
        account._account(identities[0])


def test_supplementary_privilege_is_rejected(identities, monkeypatch):
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [gid, 27])
    with pytest.raises(ValueError):
        account._account(identities[0])


@pytest.mark.parametrize("bad", ["symlink", "writable", "owner"])
def test_untrusted_ancestor_is_rejected(monkeypatch, bad):
    seen = []

    def inspect(path):
        seen.append(str(path))
        mode, uid, gid = stat.S_IFDIR | 0o755, 0, 0
        if str(path) == "/var/lib":
            if bad == "symlink":
                mode = stat.S_IFLNK | 0o777
            elif bad == "writable":
                mode, gid = mode | 0o020, 1000  # an ordinary group may write
            else:
                uid = 1000
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)

    monkeypatch.setattr(account.Path, "lstat", inspect)
    with pytest.raises(ValueError):
        account._trusted_root(account.ROOT)
    assert seen == ["/", "/var", "/var/lib"]


@pytest.fixture
def store(tmp_path, monkeypatch, identities):
    monkeypatch.setattr(account.sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(account, "ROOT", tmp_path)
    monkeypatch.setattr(account, "_trusted_root", lambda path: None)
    fstat = os.fstat

    def info(fd):
        real = fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=real.st_mode, st_nlink=real.st_nlink)

    monkeypatch.setattr(os, "fstat", info)
    path = tmp_path / "account.json"
    path.write_text(json.dumps(identities[0]))
    path.chmod(0o600)
    return path


def test_valid_private_provenance(store):
    assert account.runtime_account() == account.RuntimeAccount(991, 992)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo", "large", "malformed", "unicode"])
def test_unsafe_receipt_fails_closed(store, kind):
    if kind == "symlink":
        target = store.with_name("other")
        store.rename(target)
        store.symlink_to(target)
    elif kind == "hardlink":
        os.link(store, store.with_name("alias"))
    elif kind == "public":
        store.chmod(0o644)
    elif kind == "fifo":
        store.unlink()
        os.mkfifo(store)
    elif kind == "large":
        store.write_text(" " * 2049)
    elif kind == "malformed":
        store.write_text("[]")
    else:
        store.write_bytes(b"\xff")
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        account.runtime_account()


def test_account_without_receipt_is_not_adopted(store):
    store.unlink()
    with pytest.raises(ScreenError, match="UNPRIVILEGED_RUNTIME_REQUIRED"):
        account.runtime_account()


@pytest.mark.parametrize("platform,uid", [("darwin", 0), ("win32", 0), ("linux", 1000)])
def test_other_hosts_do_not_read_account_store(monkeypatch, platform, uid):
    monkeypatch.setattr(account.sys, "platform", platform)
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    monkeypatch.setattr(account, "_trusted_root", lambda path: pytest.fail("unsupported host"))
    with pytest.raises(ScreenError, match="UNPRIVILEGED_RUNTIME_REQUIRED"):
        account.runtime_account()
