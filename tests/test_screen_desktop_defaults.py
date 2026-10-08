"""First-run seeding preserves preferences and cannot follow supplied links."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from flowly.desktop_runtime.desktop_defaults import DESKTOP_DEFAULTS_SOURCE

pytestmark = pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="desktop seed worker requires an unprivileged POSIX application account",
)


@pytest.fixture
def application(tmp_path):
    environment = {"PATH": "/usr/bin:/bin"}
    for key, leaf in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_RUNTIME_DIR", "run")):
        path = tmp_path / leaf
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    browser = tmp_path / "browser executable"
    browser.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    browser.chmod(0o700)
    profile = tmp_path / "session $(touch attacked)"
    profile.mkdir()
    return environment, browser, profile


def seed(application):
    environment, browser, profile = application
    return subprocess.run([sys.executable, "-I", "-c", DESKTOP_DEFAULTS_SOURCE, str(browser), str(profile)],
                          env=environment, capture_output=True, text=True, timeout=10)


def test_preferences_survive_repeated_seeding(application):
    assert seed(application).returncode == 0
    environment, _, _ = application
    settings = Path(environment["XDG_CONFIG_HOME"]) / "xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"
    changed = settings.read_text().replace("Sans 10", "Sans 13")
    settings.write_text(changed)
    assert seed(application).returncode == 0
    assert settings.read_text() == changed


def test_browser_launcher_requests_only_the_managed_browser(application):
    import socket
    import tempfile

    environment, _, profile = application
    with tempfile.TemporaryDirectory(prefix="fl-launcher-", dir="/tmp") as runtime:
        environment["XDG_RUNTIME_DIR"] = runtime
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(str(Path(runtime) / "browser-open.sock"))
            server.settimeout(2)
            assert seed(application).returncode == 0
            launcher = Path(runtime) / "launch-browser"
            result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert server.recv(32) == b"open"
            assert not (profile.parent / "attacked").exists()
            launcher.write_text("obsolete launcher")
            assert seed(application).returncode == 0
            assert "obsolete launcher" not in launcher.read_text()


@pytest.mark.parametrize("target", ["config", "launcher"])
def test_seeding_refuses_links_without_changing_their_target(application, tmp_path, target):
    environment, _, _ = application
    outside = tmp_path / "outside"
    if target == "config":
        outside.mkdir(mode=0o700)
        directory = Path(environment["XDG_CONFIG_HOME"]) / "xfce4"
        directory.symlink_to(outside)
    else:
        outside.write_text("preserve this")
        (Path(environment["XDG_RUNTIME_DIR"]) / "launch-browser").symlink_to(outside)
    assert seed(application).returncode != 0
    if target == "config":
        assert list(outside.iterdir()) == []
    else:
        assert outside.read_text() == "preserve this"


def test_dock_entries_use_flowly_icons_and_replace_older_theme_names(application):
    environment, _, _ = application
    entries = Path(environment["XDG_DATA_HOME"]) / "applications"
    entries.mkdir(mode=0o700)
    old = entries / "flowly-terminal.desktop"
    old.write_text("[Desktop Entry]\nName=Terminal\nIcon=utilities-terminal\n")
    old.chmod(0o600)
    assert seed(application).returncode == 0
    for name in ("browser", "terminal", "settings"):
        entry = (entries / f"flowly-{name}.desktop").read_text()
        icon = Path(next(line[5:] for line in entry.splitlines() if line.startswith("Icon=")))
        assert icon.parent == Path(environment["XDG_DATA_HOME"]) / "flowly-dock"
        assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert icon.stat().st_mode & 0o777 == 0o600
