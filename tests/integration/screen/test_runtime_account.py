"""Real account creation only in an explicitly opted-in disposable root runner."""

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from flowly.desktop_runtime.application_layout import root_application_layout
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


@pytest.fixture
def layout(provisioned, request):
    # /root is deliberately unreadable by the runtime account; actual app data
    # must live elsewhere while the agent retains its authority/control files.
    directory = Path("/root/screen-layout-tests") / request.node.name
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    result = root_application_layout(directory, create=True)
    return directory, result


def test_root_layout_keeps_privileged_entries_outside_application_ownership(layout):
    directory, application = layout
    assert root_application_layout(directory) == application
    assert (directory / "application-layout.json").stat().st_uid == 0
    assert application.directory.stat().st_uid == 0
    assert application.sockets.parent.stat().st_uid == 0
    assert application.browser.stat().st_uid == application.account.uid
    assert application.sockets.stat().st_uid == application.account.uid
    code = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('application write')"
    child = subprocess.run(["/usr/bin/python3", "-I", "-c", code, str(application.browser / "test")],
                           user=application.account.uid, group=application.account.gid,
                           extra_groups=[], cwd="/", capture_output=True, env=application.environment())
    assert child.returncode == 0
    denied = subprocess.run(["/usr/bin/python3", "-I", "-c", code, str(directory / "authority-write")],
                            user=application.account.uid, group=application.account.gid,
                            extra_groups=[], cwd="/", capture_output=True, env=application.environment())
    assert denied.returncode != 0 and not (directory / "authority-write").exists()
    replacement = subprocess.run(["/usr/bin/python3", "-I", "-c",
                                 "import os,sys;os.rename(sys.argv[1],sys.argv[1]+'-moved')",
                                 str(application.browser)], user=application.account.uid,
                                group=application.account.gid, extra_groups=[], cwd="/",
                                capture_output=True, env=application.environment())
    assert replacement.returncode != 0 and application.browser.is_dir()


def test_readonly_layout_does_not_create_state(provisioned):
    directory = Path("/root/screen-layout-readonly")
    directory.mkdir(mode=0o700)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        root_application_layout(directory)
    assert list(directory.iterdir()) == []


def test_recreated_profile_gets_new_application_storage(layout):
    directory, previous = layout
    (directory / "application-layout.json").unlink()
    current = root_application_layout(directory, create=True)
    assert current.directory != previous.directory and current.sockets != previous.sockets


def test_copied_mapping_cannot_share_original_browser(layout):
    directory, original = layout
    target = directory.with_name(directory.name + "-copy")
    target.mkdir(mode=0o700)
    mapping = target / "application-layout.json"
    mapping.write_bytes((directory / "application-layout.json").read_bytes())
    mapping.chmod(0o600)
    for create in (False, True):
        with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
            root_application_layout(target, create=create)
    assert original.browser.exists()


@pytest.mark.parametrize("kind", ["symlink", "public", "wrong-owner"])
def test_unsafe_app_directory_is_not_followed_or_repaired(layout, kind):
    directory, application = layout
    path = application.browser
    if kind == "symlink":
        path.rmdir()
        path.symlink_to("/root")
    elif kind == "public":
        path.chmod(0o777)
    else:
        os.chown(path, 0, 0)
    for create in (False, True):
        with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
            root_application_layout(directory, create=create)
    if kind == "symlink":
        assert path.is_symlink()


@pytest.mark.parametrize("kind", ["regular", "symlink", "fifo", "large", "wrong-owner", "invalid"])
def test_browser_port_read_is_bounded_and_does_not_follow_links(layout, kind):
    _, application = layout
    path = application.browser / "DevToolsActivePort"
    assert application.read_browser_port() is None
    if kind == "symlink":
        path.symlink_to("/etc/passwd")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("12345\n/devtools/browser/test" if kind != "large" else "1" * 129)
        if kind == "invalid":
            path.write_text("65536\n")
    if kind != "wrong-owner":
        os.chown(path, application.account.uid, application.account.gid, follow_symlinks=False)
    if kind == "regular":
        assert application.read_browser_port() == 12345
    else:
        with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
            application.read_browser_port()


def test_root_agent_runs_real_browser_and_input_as_unprivileged_account(layout, monkeypatch):
    from playwright.sync_api import sync_playwright

    from flowly.desktop_runtime.input_broker import ScreenInputBroker
    from flowly.desktop_runtime.launcher import ScreenRuntime, is_alive

    directory, application = layout
    monkeypatch.setenv("CUSTOM_AGENT_TOKEN", "synthetic-root-agent-secret")
    assert "CUSTOM_AGENT_TOKEN" not in application.environment()
    assert "DBUS_SESSION_BUS_ADDRESS" not in application.environment()
    fake_programs = directory / "agent-path"
    fake_programs.mkdir()
    fake_browser = fake_programs / "chromium"
    fake_browser.write_text("#!/bin/sh\nexit 91\n")
    fake_browser.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_programs) + os.pathsep + os.environ["PATH"])
    runtime = ScreenRuntime(directory)
    assert runtime.capabilities()["supported"]
    state = runtime.start()
    generation = state["generation"]
    try:
        assert state["socketLayoutVersion"] == 3
        assert len(state["children"]) == 4  # private D-Bus, Xvnc, Openbox, Chromium
        assert Path(state["socket"]) == application.sockets / "rfb.sock"
        assert (application.sockets / "bus").stat().st_uid == application.account.uid
        assert (runtime.socket_directory / "input.sock").stat().st_uid == 0
        for identity in state["children"]:
            status = Path(f"/proc/{identity['pid']}/status").read_text()
            uid = next(line for line in status.splitlines() if line.startswith("Uid:"))
            assert set(uid.split()[1:]) == {str(application.account.uid)}
            command = Path(f"/proc/{identity['pid']}/cmdline").read_bytes()
            assert b"--no-sandbox" not in command
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['cdpPort']}")
            context = browser.contexts[0]
            page = context.pages[0]
            page.goto("data:text/html,<input id='code' autofocus>")
            point = page.locator("#code").evaluate("""element => {
                const r = element.getBoundingClientRect();
                return {x: Math.round(screenX + r.x + 10),
                        y: Math.round(screenY + outerHeight - innerHeight + r.y + 10)};
            }""")
            authority = runtime.authority
            action = authority.begin_action("root-original-run", "before", generation)
            assert authority.end_action(action)
            human = authority.acquire("root-viewer", generation, authority.snapshot()["epoch"])
            broker = ScreenInputBroker(authority, "root-viewer", generation, human["epoch"], Path(state["socket"]))
            try:
                broker.send(b"".join(struct.pack("!BBHH", 5, buttons, point["x"], point["y"])
                                     for buttons in (0, 1, 0)))
                page.wait_for_function("document.hasFocus() && document.activeElement.id === 'code'")
                broker.send(b"".join(struct.pack("!BBHI", 4, down, 0, ord(letter))
                                     for letter in "246810" for down in (1, 0)))
                page.wait_for_function("document.querySelector('#code').value === '246810'")
                released = broker.finish(release=True)
                assert released["control"] == "agent" and released["runId"] == "root-original-run"
                action = authority.begin_action("root-original-run", "after", generation)
                assert authority.end_action(action)
                authority.finish_run("root-original-run", generation)
            finally:
                broker.abandon()
            assert page.screenshot().startswith(b"\x89PNG")
            context.add_cookies([{"url": "http://screen.test", "name": "session", "value": "root-fixture",
                                  "expires": 2000000000}])
        assert runtime.start()["generation"] == generation
    finally:
        runtime.stop(generation)
    assert not any(is_alive(identity) for identity in state["children"])
    restarted = runtime.start()
    try:
        assert restarted["generation"] != generation
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{restarted['cdpPort']}")
            cookies = browser.contexts[0].cookies("http://screen.test")
            assert any(cookie["name"] == "session" and cookie["value"] == "root-fixture" for cookie in cookies)
    finally:
        runtime.stop(restarted["generation"])
