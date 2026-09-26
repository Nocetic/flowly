"""Actual Xfce applications in the disposable desktop candidate image."""

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flowly.desktop_runtime.desktop_defaults import DESKTOP_DEFAULTS_SOURCE
from flowly.desktop_runtime.supervisor import write_xauthority

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOWLY_SCREEN_DESKTOP_TEST") != "1" or sys.platform != "linux",
    reason="requires disposable Linux desktop candidate runner",
)


@pytest.fixture
def desktop(tmp_path):
    assert os.geteuid() != 0
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                   "XDG_CURRENT_DESKTOP": "XFCE", "SHELL": "/bin/bash"}
    for key, leaf in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "run")):
        path = tmp_path / leaf
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    sockets = Path(environment["XDG_RUNTIME_DIR"])
    browser_profile = tmp_path / "browser"
    browser_profile.mkdir(mode=0o700)
    # Unix socket paths have a strict length limit, independently of HOME/XDG.
    import tempfile

    short = tempfile.TemporaryDirectory(prefix="flowly-desktop-", dir="/tmp")
    sockets = Path(short.name)
    environment["XDG_RUNTIME_DIR"] = str(sockets)
    authority = sockets / "Xauthority"
    write_xauthority(authority)
    environment["XAUTHORITY"] = str(authority)
    children = []
    logs = []

    def spawn(arguments, **kwargs):
        log = (tmp_path / f"process-{len(children)}.log").open("wb")
        logs.append(log)
        child = subprocess.Popen(arguments, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=kwargs.pop("stdout", log), stderr=log,
                                 start_new_session=True, **kwargs)
        children.append(child)
        return child

    def command(*arguments):
        return subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=10)

    def wait_for(predicate):
        until = time.monotonic() + 20
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            assert all(child.poll() is None for child in children), [
                file.read_text(errors="replace") for file in tmp_path.glob("process-*.log")]
            time.sleep(0.05)
        pytest.fail("desktop condition timed out\n" + command("xwininfo", "-root", "-tree").stdout +
                    command("xprop", "-root", "_NET_ACTIVE_WINDOW").stdout + "\n" +
                    "\n".join(file.read_text(errors="replace") for file in tmp_path.glob("process-*.log")))

    def window(pattern):
        result = command("xdotool", "search", "--onlyvisible", "--class", pattern)
        return result.stdout.splitlines()[-1] if result.returncode == 0 else None

    try:
        bus = spawn(["dbus-daemon", "--session", "--nofork", "--nopidfile",
                     "--address=unix:path=" + str(sockets / "bus"), "--print-address=1"],
                    stdout=subprocess.PIPE)
        assert select.select([bus.stdout], [], [], 10)[0]
        environment["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().decode().strip()
        seeded = command("/usr/bin/python3", "-I", "-c", DESKTOP_DEFAULTS_SOURCE,
                         "/usr/bin/chromium", str(browser_profile))
        assert seeded.returncode == 0, seeded.stderr
        read_fd, write_fd = os.pipe()
        try:
            spawn(["Xvnc", "-displayfd", str(write_fd), "-geometry", "1440x900", "-depth", "24",
                   "-auth", str(authority), "-nolisten", "tcp", "-rfbport", "-1",
                   "-rfbunixpath", str(sockets / "rfb.sock"), "-rfbunixmode", "0600",
                   "-SecurityTypes", "None", "-AlwaysShared"], pass_fds=(write_fd,))
            os.close(write_fd)
            write_fd = -1
            assert select.select([read_fd], [], [], 10)[0]
            environment["DISPLAY"] = ":" + os.read(read_fd, 32).strip().decode()
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        spawn(["xfwm4", "--compositor=off"])
        spawn(["xfsettingsd"])
        spawn(["xfdesktop"])
        spawn(["xfce4-panel"])
        panel = wait_for(lambda: window("^xfce4-panel$"))
        spawn(["chromium", "--user-data-dir=" + str(browser_profile),
               "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
               "--no-first-run", "--no-default-browser-check", "--window-size=1100,700", "about:blank"])
        wait_for(lambda: window("^chromium$"))
        yield dict(env=environment, command=command, wait_for=wait_for, window=window,
                   panel=panel, profile=browser_profile, root=tmp_path)
    finally:
        for child in reversed(children):
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
        for log in logs:
            log.close()
        short.cleanup()


def test_dock_opens_terminal_and_settings_and_focuses_same_browser(desktop):
    from playwright.sync_api import sync_playwright

    command, wait_for, window = (desktop[key] for key in ("command", "wait_for", "window"))
    geometry = command("xdotool", "getwindowgeometry", "--shell", desktop["panel"])
    assert geometry.returncode == 0, geometry.stderr
    bounds = dict(line.split("=", 1) for line in geometry.stdout.splitlines())
    assert int(bounds["Y"]) >= 800 and int(bounds["HEIGHT"]) >= 40

    def click_launcher(index):
        result = command("xdotool", "mousemove", str(int(bounds["X"]) + 24 + index * 48),
                         str(int(bounds["Y"]) + 24), "click", "1")
        assert result.returncode == 0

    click_launcher(1)
    wait_for(lambda: window("^xfce4-terminal$"))
    typed = command("xdotool", "type", "--clearmodifiers", "--delay", "5",
                    'printf desktop-terminal-ready > "$HOME/terminal-probe"')
    assert typed.returncode == 0
    assert command("xdotool", "key", "Return").returncode == 0
    probe = Path(desktop["env"]["HOME"]) / "terminal-probe"
    wait_for(probe.exists)
    assert probe.read_text() == "desktop-terminal-ready"

    click_launcher(2)
    wait_for(lambda: window("^xfce4-settings-manager$"))
    assert command("xfconf-query", "-c", "xsettings", "-p", "/Gtk/FontName", "-s", "Sans 11").returncode == 0
    assert command("xfconf-query", "-c", "xsettings", "-p", "/Gtk/FontName").stdout.strip() == "Sans 11"
    settings_file = Path(desktop["env"]["XDG_CONFIG_HOME"]) / "xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"
    wait_for(lambda: "Sans 11" in settings_file.read_text())
    saved_settings = settings_file.read_bytes()
    reseeded = command("/usr/bin/python3", "-I", "-c", DESKTOP_DEFAULTS_SOURCE,
                       "/usr/bin/chromium", str(desktop["profile"]))
    assert reseeded.returncode == 0, reseeded.stderr
    assert settings_file.read_bytes() == saved_settings

    port_file = desktop["profile"] / "DevToolsActivePort"
    wait_for(lambda: port_file.exists() and port_file.stat().st_size)
    port = int(port_file.read_text().splitlines()[0])
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        context.add_cookies([{"url": "http://screen.test", "name": "session", "value": "same-browser"}])
        pages = len(context.pages)
        click_launcher(0)
        def browser_focused():
            active = command("xdotool", "getactivewindow")
            if active.returncode:
                return False
            return '"chromium"' in command("xprop", "-id", active.stdout.strip(), "WM_CLASS").stdout.lower()

        wait_for(browser_focused)
        assert command("xdotool", "getactivewindow", "windowminimize").returncode == 0
        click_launcher(0)
        wait_for(browser_focused)
        assert len(context.pages) == pages
        assert context.cookies("http://screen.test")[0]["value"] == "same-browser"
