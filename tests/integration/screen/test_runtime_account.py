"""Real account creation only in an explicitly opted-in disposable root runner."""

import json
import os
import subprocess
import sys

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.runtime_account import ROOT, runtime_account
from flowly.desktop_runtime.runtime_account_payload import PROVISION_SOURCE

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOWLY_SCREEN_ACCOUNT_TEST") != "1" or sys.platform != "linux",
    reason="requires disposable root Linux account runner",
)


def worker():
    return subprocess.run(
        ["/usr/bin/python3", "-I", "-c", PROVISION_SOURCE +
         '\nprint(json.dumps(provision_runtime_account()))\n'],
        capture_output=True, text=True, timeout=35, check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})


@pytest.fixture(scope="module")
def provisioned():
    import grp
    import pwd

    assert os.geteuid() == 0
    assert not ROOT.exists(), "This test requires a fresh disposable container"
    with pytest.raises(KeyError):
        pwd.getpwnam("flowly-screen")
    with pytest.raises(KeyError):
        grp.getgrnam("flowly-screen")
    with pytest.raises(ScreenError, match="UNPRIVILEGED_RUNTIME_REQUIRED"):
        runtime_account()
    result = worker()
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["uid"] > 0 and value["gid"] > 0
    assert value["shell"] == "/usr/sbin/nologin" and value["home"] == "/nonexistent"
    info = (ROOT / "account.json").stat()
    assert info.st_uid == 0 and info.st_mode & 0o777 == 0o600
    status = subprocess.run(["/usr/bin/passwd", "-S", "flowly-screen"],
                            check=True, capture_output=True, text=True)
    assert status.stdout.split()[1] == "L"
    assert not (ROOT / "account.pending").exists()
    return value


def test_real_creation_and_idempotent_receipt(provisioned):
    account = runtime_account()
    assert account.uid == provisioned["uid"] and account.gid == provisioned["gid"]
    before = (ROOT / "account.json").stat()
    again = worker()
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == provisioned
    after = (ROOT / "account.json").stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize("raw", ["null", "[]", '{"accountVersion":true}', "{broken"])
def test_invalid_existing_provenance_is_not_replaced(provisioned, raw):
    path = ROOT / "account.json"
    original = path.read_bytes()
    try:
        path.write_text(raw)
        assert worker().returncode != 0
        with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
            runtime_account()
        assert path.read_text() == raw
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize("pending", [False, True])
def test_preexisting_account_without_provenance_is_not_adopted(provisioned, pending):
    path, marker = ROOT / "account.json", ROOT / "account.pending"
    original = path.read_bytes()
    path.unlink()
    try:
        if pending:
            marker.write_text('{"accountVersion":1,"state":"creating"}')
            marker.chmod(0o600)
        result = worker()
        assert result.returncode != 0 and "INSTALL_RECOVERY_REQUIRED" in result.stderr
        assert not path.exists()
        if pending:
            assert marker.exists()
    finally:
        path.write_bytes(original)
        path.chmod(0o600)
        marker.unlink(missing_ok=True)


def test_account_with_added_privileges_is_rejected(provisioned):
    subprocess.run(["/usr/sbin/usermod", "-a", "-G", "root", "flowly-screen"], check=True)
    try:
        with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
            runtime_account()
        result = worker()
        assert result.returncode != 0 and "INSTALL_STATE_INVALID" in result.stderr
    finally:
        subprocess.run(["/usr/sbin/usermod", "-G", "", "flowly-screen"], check=True)
    assert runtime_account().uid == provisioned["uid"]


def test_parallel_provisioner_cannot_pass_account_lock(provisioned):
    import fcntl

    with (ROOT / "account.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = worker()
        assert result.returncode != 0 and "INSTALL_BUSY" in result.stderr
    assert runtime_account().uid == provisioned["uid"]


@pytest.mark.parametrize("name", ["account.lock", "account.json"])
def test_linked_privileged_files_are_not_followed(provisioned, name):
    path = ROOT / name
    backup = path.with_suffix(".test-backup")
    path.rename(backup)
    path.symlink_to(backup)
    try:
        assert worker().returncode != 0
        assert path.is_symlink()
        if name == "account.json":
            with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
                runtime_account()
    finally:
        path.unlink()
        backup.rename(path)
