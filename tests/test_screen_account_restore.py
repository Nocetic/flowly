"""Boot-time recreation of receipted screen accounts after a root filesystem reset.

The payload runs as root in an isolated interpreter on the host; here its logic
runs in-process against an in-memory account database, so every decision
(recreate, keep, refuse) is checked without touching the real /etc/passwd.
The real useradd/groupadd path runs in tests/integration/screen.
"""

import json
import os
import subprocess
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowly.desktop_runtime import profile_accounts
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.runtime_account_payload import PROFILE_ACCOUNT_SOURCE

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix account payload")

NAME = "flowly-s-0123456789ab"


class Accounts:
    """Minimal pwd/grp/useradd/groupadd stand-in."""

    def __init__(self):
        self.users, self.groups, self.locked, self.commands = {}, {}, set(), []
        self.fail = set()

    def add(self, name, uid, gid, *, group=True, home="/nonexistent", shell="/usr/sbin/nologin", locked=True):
        self.users[name] = SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid, pw_dir=home, pw_shell=shell)
        if group:
            self.groups[name] = SimpleNamespace(gr_name=name, gr_gid=gid, gr_mem=[])
        if locked:
            self.locked.add(name)

    def pwd(self):
        def getpwnam(name):
            return self.users[name]

        def getpwuid(uid):
            for user in self.users.values():
                if user.pw_uid == uid:
                    return user
            raise KeyError(uid)
        return types.SimpleNamespace(getpwnam=getpwnam, getpwuid=getpwuid)

    def grp(self):
        def getgrnam(name):
            return self.groups[name]

        def getgrgid(gid):
            for group in self.groups.values():
                if group.gr_gid == gid:
                    return group
            raise KeyError(gid)
        return types.SimpleNamespace(getgrnam=getgrnam, getgrgid=getgrgid)

    def run(self, command):
        self.commands.append(command)
        tool = Path(command[0]).name
        if tool in self.fail:
            return 1
        name = command[-1]
        option = {command[i]: command[i + 1] for i in range(1, len(command) - 1) if command[i].startswith("--")
                  and not command[i + 1].startswith("--")}
        if tool == "groupadd":
            self.groups[name] = SimpleNamespace(gr_name=name, gr_gid=int(option["--gid"]), gr_mem=[])
        elif tool == "useradd":
            self.add(name, int(option["--uid"]), int(option["--gid"]), group=False,
                     home=option["--home-dir"], shell=option["--shell"], locked=option.get("--password") == "!")
        return 0


@pytest.fixture
def host(monkeypatch, tmp_path):
    accounts = Accounts()
    worker = types.ModuleType("restore")
    exec(compile(PROFILE_ACCOUNT_SOURCE, "<restore>", "exec"), worker.__dict__)
    worker.pwd, worker.grp = accounts.pwd(), accounts.grp()
    worker.account_run = accounts.run
    worker.account_password_locked = lambda name: name in accounts.locked
    worker.account_system_uid = lambda uid: 0 < uid < 1000
    # Ownership and mode checks of receipts are covered by the provisioner tests.
    worker.account_read = lambda path: json.loads(Path(path).read_text())
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [gid])
    saved = []

    def save(path, value):
        Path(path).write_text(json.dumps(value))
        saved.append(Path(path).name)
    return SimpleNamespace(worker=worker, accounts=accounts, dir=tmp_path, save=save, saved=saved)


def receipt(name=NAME, uid=995, gid=994, **changes):
    return {"accountVersion": 1, "name": name, "uid": uid, "gid": gid,
            "home": "/nonexistent", "shell": "/usr/sbin/nologin", **changes}


def restore(host, value=None, name=NAME):
    marker = host.dir / (name + ".restoring")
    return host.worker.account_restore(
        name, receipt() if value is None else value, marker,
        lambda value: host.worker.profile_account_identity(name, value), host.save, lambda: None)


def code(host, value=None):
    with pytest.raises(ValueError) as caught:
        restore(host, value)
    return str(caught.value)


def test_missing_account_is_recreated_with_the_receipted_ids(host):
    assert restore(host) == "restored"
    user, group = host.accounts.users[NAME], host.accounts.groups[NAME]
    assert (user.pw_uid, user.pw_gid, group.gr_gid) == (995, 994, 994)
    assert (user.pw_dir, user.pw_shell) == ("/nonexistent", "/usr/sbin/nologin") and NAME in host.accounts.locked
    assert [Path(command[0]).name for command in host.accounts.commands] == ["groupadd", "useradd"]
    # The reservation is written before anything changes and removed after.
    assert host.saved == [NAME + ".restoring"] and not (host.dir / (NAME + ".restoring")).exists()


def test_exact_existing_account_is_kept_without_changes(host):
    host.accounts.add(NAME, 995, 994)
    assert restore(host) == "present"
    assert host.accounts.commands == [] and host.saved == []


def test_restoring_twice_is_idempotent(host):
    assert restore(host) == "restored"
    assert restore(host) == "present"
    assert len(host.accounts.commands) == 2


@pytest.mark.parametrize("change", [
    dict(uid=996), dict(gid=993), dict(home="/root"), dict(shell="/bin/bash"), dict(locked=False),
])
def test_differing_existing_account_is_never_adopted(host, change):
    values = dict(uid=995, gid=994)
    values.update({key: value for key, value in change.items() if key in values})
    host.accounts.add(NAME, values["uid"], values["gid"],
                      **{key: value for key, value in change.items() if key not in values})
    assert code(host) == "INSTALL_ACCOUNT_CONFLICT"
    assert host.accounts.commands == []


def test_uid_held_by_another_account_is_not_taken(host):
    host.accounts.add("_chrony", 995, 120)
    assert code(host) == "INSTALL_ACCOUNT_CONFLICT"
    assert host.accounts.commands == [] and host.saved == []


def test_gid_held_by_another_group_is_not_taken(host):
    host.accounts.groups["render"] = SimpleNamespace(gr_name="render", gr_gid=994, gr_mem=[])
    assert code(host) == "INSTALL_ACCOUNT_CONFLICT"
    assert host.accounts.commands == []


def test_user_without_its_group_is_a_conflict(host):
    host.accounts.add(NAME, 995, 994, group=False)
    assert code(host) == "INSTALL_ACCOUNT_CONFLICT"


def test_a_group_this_restorer_never_reserved_is_not_adopted(host):
    host.accounts.groups[NAME] = SimpleNamespace(gr_name=NAME, gr_gid=994, gr_mem=[])
    assert code(host) == "INSTALL_ACCOUNT_CONFLICT"
    assert host.accounts.commands == []


def test_interrupted_restore_after_groupadd_is_completed(host):
    host.accounts.fail.add("useradd")
    assert code(host) == "INSTALL_RECOVERY_REQUIRED"
    assert (host.dir / (NAME + ".restoring")).exists() and NAME in host.accounts.groups
    host.accounts.fail.clear()
    assert restore(host) == "restored"
    assert [Path(command[0]).name for command in host.accounts.commands] == ["groupadd", "useradd", "useradd"]
    assert not (host.dir / (NAME + ".restoring")).exists()


def test_interrupted_restore_after_useradd_only_clears_its_reservation(host):
    host.accounts.add(NAME, 995, 994)
    host.save(host.dir / (NAME + ".restoring"), {"accountVersion": 1, "state": "restoring", "uid": 995, "gid": 994})
    assert restore(host) == "restored"
    assert host.accounts.commands == [] and not (host.dir / (NAME + ".restoring")).exists()


def test_reservation_for_other_ids_is_corrupt_state(host):
    host.save(host.dir / (NAME + ".restoring"), {"accountVersion": 1, "state": "restoring", "uid": 0, "gid": 0})
    assert code(host) == "INSTALL_STATE_INVALID"
    assert host.accounts.commands == []


def test_reserved_group_with_members_is_not_adopted(host):
    host.save(host.dir / (NAME + ".restoring"), {"accountVersion": 1, "state": "restoring", "uid": 995, "gid": 994})
    host.accounts.groups[NAME] = SimpleNamespace(gr_name=NAME, gr_gid=994, gr_mem=["intruder"])
    assert code(host) == "INSTALL_ACCOUNT_CONFLICT"
    assert host.accounts.commands == []


@pytest.mark.parametrize("value", [
    receipt(uid=0), receipt(gid=0), receipt(uid=1000), receipt(gid=65534), receipt(uid=True),
    receipt(uid="995"), receipt(name="flowly-s-ffffffffffff"), receipt(home="/root"),
    receipt(shell="/bin/sh"), receipt(accountVersion=2), {**receipt(), "extra": 1}, [],
])
def test_receipt_cannot_choose_an_arbitrary_account(host, value):
    assert code(host, value) == "INSTALL_STATE_INVALID"
    assert host.accounts.commands == []


def test_failed_groupadd_leaves_the_reservation(host):
    host.accounts.fail.add("groupadd")
    assert code(host) == "INSTALL_RECOVERY_REQUIRED"
    assert (host.dir / (NAME + ".restoring")).exists() and NAME not in host.accounts.users


def test_useradd_that_does_not_produce_the_receipt_is_recovery(host):
    original = host.accounts.run

    def wrong(command):
        result = original(command)
        if Path(command[0]).name == "useradd":
            host.accounts.users[NAME].pw_shell = "/bin/bash"
        return result
    host.worker.account_run = wrong
    assert code(host) == "INSTALL_RECOVERY_REQUIRED"


def test_useradd_command_pins_ids_and_locks_the_password(host):
    restore(host)
    groupadd, useradd = host.accounts.commands
    assert groupadd == ["/usr/sbin/groupadd", "--system", "--gid", "994", NAME]
    assert useradd == ["/usr/sbin/useradd", "--system", "--no-user-group", "--no-create-home", "--uid", "995",
                       "--gid", "994", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
                       "--password", "!", NAME]


# ── Orchestration over the shared and per-profile receipts ──────────────────


@pytest.fixture
def store(host, monkeypatch):
    root = host.dir / "runtime"
    (root / "accounts").mkdir(parents=True)
    worker = host.worker
    worker.ACCOUNT_ROOT, worker.PROFILE_ACCOUNTS = root, root / "accounts"
    worker.account_directory = lambda path, create=False: Path(path).lstat() and None
    worker.account_lock_wait = lambda path: os.open(os.devnull, os.O_RDONLY)
    worker.profile_account_locked = lambda work: work()
    worker.account_save = worker.profile_account_save = host.save
    worker.account_sync = worker.profile_account_sync = lambda: None
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    return root


def test_host_without_screen_install_reports_absent(host, store):
    (store / "accounts").rmdir()
    store.rmdir()
    assert host.worker.restore_runtime_accounts() == {"shared": "absent", "profiles": {}}
    assert host.accounts.commands == []


def test_shared_account_then_each_profile_is_restored(host, store):
    (store / "account.json").write_text(json.dumps(receipt("flowly-screen", 999, 999)))
    good, taken, broken = "flowly-s-aaaaaaaaaaaa", "flowly-s-bbbbbbbbbbbb", "flowly-s-cccccccccccc"
    (store / "accounts" / (good + ".json")).write_text(json.dumps(receipt(good, 998, 997)))
    (store / "accounts" / (taken + ".json")).write_text(json.dumps(receipt(taken, 996, 995)))
    (store / "accounts" / (broken + ".json")).write_text("{broken")
    for ignored in ("README.json", good + ".pending", "accounts.lock", "flowly-s-DDDDDDDDDDDD.json"):
        (store / "accounts" / ignored).write_text("{}")
    host.accounts.add("_apt", 996, 65534, group=False)
    report = host.worker.restore_runtime_accounts()
    assert report == {"shared": "restored", "profiles": {
        good: "restored", taken: "INSTALL_ACCOUNT_CONFLICT", broken: "INSTALL_STATE_INVALID"}}
    assert host.accounts.users["flowly-screen"].pw_uid == 999 and host.accounts.users[good].pw_uid == 998
    assert taken not in host.accounts.users


def test_shared_account_conflict_stops_before_profiles(host, store):
    (store / "account.json").write_text(json.dumps(receipt("flowly-screen", 999, 999)))
    (store / "accounts" / (NAME + ".json")).write_text(json.dumps(receipt()))
    host.accounts.add("flowly-screen", 990, 990)  # The image created its own, with other ids.
    with pytest.raises(ValueError, match="INSTALL_ACCOUNT_CONFLICT"):
        host.worker.restore_runtime_accounts()
    assert NAME not in host.accounts.users


def test_restore_requires_root(host, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="INSTALL_PRIVILEGE_REQUIRED"):
        host.worker.restore_runtime_accounts()


# ── Agent-side wrapper ──────────────────────────────────────────────────────


def completed(value):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(value).encode(), stderr=b"")


def test_wrapper_maps_payload_codes_to_screen_codes(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, timeout=kwargs["timeout"], env=kwargs["env"])
        return completed({"ok": True, "value": {"shared": "present", "profiles": {
            "flowly-s-aaaaaaaaaaaa": "restored", "flowly-s-bbbbbbbbbbbb": "INSTALL_ACCOUNT_CONFLICT",
            "flowly-s-cccccccccccc": "SOMETHING_ELSE"}}})
    monkeypatch.setattr(profile_accounts.subprocess, "run", run)
    assert profile_accounts.restore_runtime_accounts() == {"shared": "present", "profiles": {
        "flowly-s-aaaaaaaaaaaa": "restored", "flowly-s-bbbbbbbbbbbb": "RUNTIME_ACCOUNT_CONFLICT",
        "flowly-s-cccccccccccc": "RUNTIME_STATE_INVALID"}}
    assert seen["command"][:3] == ["/usr/bin/python3", "-I", "-c"]
    assert "restore_runtime_accounts(sys.argv[1])" in seen["command"][3]
    assert set(seen["env"]) == {"PATH", "LANG", "LC_ALL"}


@pytest.mark.parametrize("value", [
    {"error": "INSTALL_ACCOUNT_CONFLICT"},
    {"ok": True, "value": None},
    {"ok": True, "value": {"shared": "maybe", "profiles": {}}},
    {"ok": True, "value": {"shared": "present", "profiles": {"../../etc": "restored"}}},
    {"ok": True, "value": {"shared": "present", "profiles": {}, "extra": 1}},
])
def test_wrapper_fails_closed_on_unexpected_payload_output(monkeypatch, value):
    monkeypatch.setattr(profile_accounts.subprocess, "run", lambda *a, **k: completed(value))
    with pytest.raises(ScreenError) as caught:
        profile_accounts.restore_runtime_accounts()
    assert caught.value.code in {"RUNTIME_ACCOUNT_CONFLICT", "RUNTIME_STATE_INVALID"}


def test_existing_profile_payload_calls_keep_their_contract(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command[4], kwargs["timeout"]))
        return completed({"ok": True, "value": None})
    monkeypatch.setattr(profile_accounts.subprocess, "run", run)
    profile_accounts.retire_profile_account(NAME)
    profile_accounts.terminate_profile_account_processes(NAME)
    assert calls == [(NAME, 60), (NAME, 60)]
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        profile_accounts.retire_profile_account("root")
    monkeypatch.setattr(profile_accounts.subprocess, "run",
                        lambda *a, **k: completed({"error": "INSTALL_BUSY"}))
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        profile_accounts.retire_profile_account(NAME)
