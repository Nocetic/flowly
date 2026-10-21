"""Real recreation of receipted screen accounts after a simulated rootfs reset.

Only in an explicitly opted-in disposable root container: it creates, deletes
and recreates real system accounts. A cloud machine's root filesystem is reset
on redeploy or cold start while /var/lib/flowly-screen-runtime lives on its
volume; this deletes the accounts the same way and restores them from the
receipts.
"""

import json
import os
import subprocess
import sys

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.profile_accounts import (
    profile_account,
    provision_profile_account,
    restore_runtime_accounts,
)
from flowly.desktop_runtime.runtime_account import ROOT, runtime_account
from flowly.desktop_runtime.runtime_account_payload import PROVISION_SOURCE

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOWLY_SCREEN_ACCOUNT_TEST") != "1" or sys.platform != "linux",
    reason="requires disposable root Linux account runner",
)

ALPHA, BETA = "flowly-s-a1a1a1a1a1a1", "flowly-s-b2b2b2b2b2b2"


def identity(name):
    import grp
    import pwd

    user, group = pwd.getpwnam(name), grp.getgrnam(name)
    return user.pw_uid, user.pw_gid, group.gr_gid, user.pw_dir, user.pw_shell


def userdel(name):
    subprocess.run(["/usr/sbin/userdel", name], check=True)
    subprocess.run(["/usr/sbin/groupdel", name], check=False, stderr=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def accounts():
    assert os.geteuid() == 0
    assert not ROOT.exists(), "This test requires a fresh disposable container"
    result = subprocess.run(["/usr/bin/python3", "-I", "-c", PROVISION_SOURCE + "\nprovision_runtime_account()\n"],
                            capture_output=True, text=True, timeout=35, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    assert result.returncode == 0, result.stderr
    provision_profile_account(ALPHA)
    provision_profile_account(BETA)
    return {name: identity(name) for name in ("flowly-screen", ALPHA, BETA)}


def test_accounts_lost_with_the_root_filesystem_come_back_with_the_same_ids(accounts):
    for name in accounts:
        userdel(name)
    with pytest.raises(ScreenError):
        runtime_account()
    assert restore_runtime_accounts() == {"shared": "restored", "profiles": {ALPHA: "restored", BETA: "restored"}}
    assert {name: identity(name) for name in accounts} == accounts
    shared = runtime_account()
    assert (shared.uid, shared.gid) == accounts["flowly-screen"][:2]
    assert (profile_account(ALPHA).uid, profile_account(BETA).uid) == (accounts[ALPHA][0], accounts[BETA][0])
    status = subprocess.run(["/usr/bin/passwd", "-S", ALPHA], check=True, capture_output=True, text=True)
    assert status.stdout.split()[1] == "L"
    assert not list(ROOT.glob("**/*.restoring"))
    # Every later boot finds them in place and changes nothing.
    assert restore_runtime_accounts() == {"shared": "present", "profiles": {ALPHA: "present", BETA: "present"}}


def test_an_id_someone_else_now_holds_is_never_taken(accounts):
    userdel(BETA)
    subprocess.run(["/usr/sbin/useradd", "--system", "--no-create-home", "--uid", str(accounts[BETA][0]),
                    "squatter"], check=True)
    try:
        report = restore_runtime_accounts()
        assert report == {"shared": "present", "profiles": {ALPHA: "present", BETA: "RUNTIME_ACCOUNT_CONFLICT"}}
        import pwd

        assert pwd.getpwuid(accounts[BETA][0]).pw_name == "squatter"
        with pytest.raises(KeyError):
            pwd.getpwnam(BETA)
        with pytest.raises(ScreenError):
            profile_account(BETA)
    finally:
        userdel("squatter")
    assert restore_runtime_accounts()["profiles"][BETA] == "restored"
    assert identity(BETA) == accounts[BETA]


def test_a_shared_account_with_other_ids_stops_the_restore(accounts):
    userdel("flowly-screen")
    userdel(ALPHA)
    subprocess.run(["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home", "--home-dir",
                    "/nonexistent", "--shell", "/usr/sbin/nologin", "--uid", str(accounts["flowly-screen"][0] - 20),
                    "flowly-screen"], check=True)
    try:
        with pytest.raises(ScreenError, match="RUNTIME_ACCOUNT_CONFLICT"):
            restore_runtime_accounts()
        import pwd

        with pytest.raises(KeyError):
            pwd.getpwnam(ALPHA)
    finally:
        userdel("flowly-screen")
    assert restore_runtime_accounts() == {"shared": "restored", "profiles": {ALPHA: "restored", BETA: "present"}}
    assert {name: identity(name) for name in accounts} == accounts


def test_cli_reports_restore_and_fails_on_a_profile_conflict(accounts):
    from typer.testing import CliRunner

    from flowly.cli.screen_cmd import screen_app

    userdel(ALPHA)
    result = CliRunner().invoke(screen_app, ["restore-accounts"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"shared": "present", "profiles": {ALPHA: "restored", BETA: "present"}}
    receipt = ROOT / "accounts" / (BETA + ".json")
    original = receipt.read_bytes()
    userdel(BETA)
    receipt.write_text(json.dumps({**json.loads(original), "uid": 0}))
    try:
        result = CliRunner().invoke(screen_app, ["restore-accounts"])
        assert result.exit_code == 1
        assert json.loads(result.output)["profiles"][BETA] == "RUNTIME_STATE_INVALID"
    finally:
        receipt.write_bytes(original)
    assert restore_runtime_accounts()["profiles"][BETA] == "restored"
