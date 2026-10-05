"""The privileged pinned-browser steps, run for real against a temporary root.

Root ownership is emulated (as in the worker tests); downloads come from an
in-memory opener. Nothing reaches the network.
"""

import hashlib
import io
import json
import os
import stat
import types
import zipfile
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime.install_recipe import BROWSER_SOURCE, RECIPE_SOURCE

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX installation state")


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            bundle.writestr(info, data)
    return buffer.getvalue()


GOOD = archive([("chrome-linux64/", b"", stat.S_IFDIR | 0o755),
                ("chrome-linux64/chrome", b"#!binary", stat.S_IFREG | 0o4755),
                ("chrome-linux64/resources.pak", b"data", stat.S_IFREG | 0o666)])


@pytest.fixture
def worker(tmp_path, monkeypatch):
    module = types.ModuleType("screen_browser_test")
    exec(compile(RECIPE_SOURCE + BROWSER_SOURCE, "<screen-browser>", "exec"), module.__dict__)
    module.BROWSER_ROOT = str(tmp_path / "opt" / "flowly-screen" / "chromium")
    module.PINNED_BROWSER = {"version": "1.2.3", "x86_64": {
        "directory": "chrome-linux64", "size": len(GOOD), "sha256": hashlib.sha256(GOOD).hexdigest(),
        "urls": ["https://first.example/chrome.zip", "https://second.example/chrome.zip"]}}
    # Test files are owned by the developer; emulate only their kernel UID.
    lstat, fstat = module.Path.lstat, os.fstat
    def root(info, inside=True):
        fields = list(info)
        fields[4] = 0
        if not inside:
            fields[0] &= ~0o022  # e.g. /tmp: the real ancestors are /, /opt
        return os.stat_result(fields)
    monkeypatch.setattr(module.Path, "lstat", lambda path: root(lstat(path), str(path).startswith(str(tmp_path))))
    monkeypatch.setattr(module.os, "fstat", lambda fd: root(fstat(fd)))
    module.served = {"https://first.example/chrome.zip": GOOD, "https://second.example/chrome.zip": GOOD}
    def opener(*handlers):
        def open_(request, timeout):
            data = module.served[request.full_url]
            if isinstance(data, Exception):
                raise data
            return io.BytesIO(data)
        return types.SimpleNamespace(open=open_)
    monkeypatch.setattr(module._browser_urllib, "build_opener", opener)
    module.command = Mock(return_value=(0, b""))
    return module


def published(worker):
    root = worker.Path(worker.BROWSER_ROOT)
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def test_the_verified_browser_is_published_with_its_manifest_last(worker):
    worker.install_pinned_browser("x86_64")
    root = worker.Path(worker.BROWSER_ROOT)
    executable = root / "1.2.3" / "chrome-linux64" / "chrome"
    assert json.loads((root / "browser.json").read_text()) == {
        "browserVersion": 1, "version": "1.2.3", "executable": str(executable)}
    assert published(worker) == ["1.2.3", "browser.json"]  # no temporary files left
    # setuid from the archive is never kept; data files are not writable by others.
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    assert stat.S_IMODE((executable.parent / "resources.pak").stat().st_mode) == 0o644
    assert worker.browser_manifest_valid("1.2.3", "x86_64")


def test_an_installed_browser_is_not_downloaded_again(worker):
    worker.install_pinned_browser("x86_64")
    worker.served.clear()  # any download now would fail
    worker.install_pinned_browser("x86_64")


def test_a_broken_mirror_falls_back_to_the_next(worker):
    worker.served["https://first.example/chrome.zip"] = OSError("unreachable")
    worker.install_pinned_browser("x86_64")
    assert worker.browser_manifest_valid("1.2.3", "x86_64")


@pytest.mark.parametrize("bad", [GOOD[:-1] + b"x", GOOD + b"extra", b"", ValueError("http")])
def test_bytes_that_are_not_exactly_the_pinned_ones_are_never_used(worker, bad):
    worker.served = {url: bad for url in worker.served}
    with pytest.raises(worker.BrowserInstallError) as failure:
        worker.install_pinned_browser("x86_64")
    assert failure.value.code == "INSTALL_DOWNLOAD_FAILED"
    assert published(worker) == []  # the temporary download is gone too


@pytest.mark.parametrize("entries", [
    [("chrome-linux64/../../escape", b"x", stat.S_IFREG | 0o644)],
    [("/etc/passwd", b"x", stat.S_IFREG | 0o644)],
    [("other-top/chrome", b"x", stat.S_IFREG | 0o755)],
    [("chrome-linux64/link", b"/etc/shadow", stat.S_IFLNK | 0o777)],
])
def test_an_archive_that_could_write_elsewhere_publishes_nothing(worker, entries):
    data = archive(entries)
    worker.PINNED_BROWSER["x86_64"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    worker.served = {url: data for url in worker.served}
    with pytest.raises(worker.BrowserInstallError) as failure:
        worker.install_pinned_browser("x86_64")
    assert failure.value.code == "INSTALL_BROWSER_FAILED"
    assert published(worker) == []


def test_a_manifest_that_does_not_match_is_not_trusted(worker):
    worker.install_pinned_browser("x86_64")
    manifest = worker.Path(worker.BROWSER_ROOT) / "browser.json"
    manifest.write_text(json.dumps({"browserVersion": 1, "version": "1.2.3", "executable": "/usr/bin/evil"}))
    assert not worker.browser_manifest_valid("1.2.3", "x86_64")


@pytest.mark.parametrize("abi", [True, False])
def test_the_apparmor_profile_grants_namespaces_to_the_pinned_browser_only(worker, abi):
    text = worker.apparmor_profile_text(abi)
    assert ("abi <abi/4.0>," in text) is abi
    assert f"profile flowly-screen-chromium {worker.BROWSER_ROOT}/*/chrome-linux*/chrome flags=(unconfined)" in text
    assert "  userns,\n" in text and "no-sandbox" not in text


def test_an_endless_download_stops_at_the_pinned_size(worker):
    """A mirror that keeps sending cannot fill the disk: the read stops right
    after the pinned size, not when the sender stops (here after 64 MiB)."""
    sent = []
    class Endless:
        def __init__(self):
            self.total = 0
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            sent.append(self.total)
            return False
        def read(self, size):
            if self.total >= 64 << 20:
                return b""
            self.total += size
            return b"x" * size
    worker._browser_urllib.build_opener = lambda *handlers: types.SimpleNamespace(open=lambda request, timeout: Endless())
    with pytest.raises(worker.BrowserInstallError):
        worker.install_pinned_browser("x86_64")
    assert published(worker) == []
    assert len(sent) == 2 and all(total <= 1 << 20 for total in sent)  # one chunk per mirror


@pytest.mark.parametrize("root,pinned,found,expected", [
    (True, True, {"chromium": "/usr/bin/chromium"}, "pinned"),
    (True, False, {"chromium": "/usr/bin/chromium"}, "/usr/bin/chromium"),
    (True, False, {"chromium-browser": "/usr/bin/chromium-browser"}, None),  # Ubuntu's snap shim
    (True, False, {}, None),
    (False, False, {"chromium": "/usr/local/bin/chromium"}, "/usr/local/bin/chromium"),  # the agent's PATH, as before
    (False, False, {"chromium": "/snap/bin/chromium"}, None),  # never a snap
    (True, False, {"chromium-browser": "/usr/bin/chromium-browser"}, "fedora"),  # Fedora/EPEL wrapper
])
def test_a_screen_runs_the_published_pinned_browser_else_the_distributions(monkeypatch, root, pinned, found, expected):
    from flowly.desktop_runtime import install_recipe

    monkeypatch.setitem(install_recipe._BROWSER, "browser_manifest_valid", lambda version, machine: pinned)
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr(install_recipe.os, "geteuid", lambda: 0 if root else 1000)
    def which(name, path=None):
        system = path == "/usr/bin:/bin"
        assert system is root
        return found.get(name) if not root or found.get(name, "").startswith("/usr/bin/") else None
    monkeypatch.setattr("shutil.which", which)
    # Ubuntu's chromium-browser starts the snap; Fedora's runs its own build.
    def content(path):
        if path.endswith("chromium-browser"):
            return (b"#!/usr/bin/bash\nexec /usr/lib64/chromium-browser/chromium-browser \"$@\"\n" if expected == "fedora"
                    else b"#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n")
        return b"\x7fELF\x02\x01\x01"  # a real executable
    monkeypatch.setattr(install_recipe, "open", lambda path, mode="r": __import__("io").BytesIO(content(path)), raising=False)
    result = install_recipe.screen_browser()
    if expected == "fedora":
        assert result == "/usr/bin/chromium-browser"
    elif expected == "pinned":
        assert result == f"{install_recipe.BROWSER_ROOT}/145.0.7632.6/chrome-linux64/chrome"
    else:
        assert result == expected


def test_without_any_browser_the_screen_says_chromium_is_missing(monkeypatch):
    from flowly.desktop_runtime import install_recipe, launcher

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid: 1, raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name, **kw: "/usr/bin/" + name)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(install_recipe, "screen_browser", lambda: None)
    assert launcher.ScreenRuntime.capabilities() == {"supported": False, "reason": "DEPENDENCIES_MISSING",
                                                     "missing": ["chromium"]}


def test_the_browser_is_started_with_one_window_class_and_no_testing_bar_for_the_pinned_build():
    from flowly.desktop_runtime.browser_lifecycle import BROWSER_MANAGER_SOURCE

    assert '"--class=chromium"' in BROWSER_MANAGER_SOURCE
    assert '["--test-type"] if browser.startswith("/opt/flowly-screen/chromium/")' in BROWSER_MANAGER_SOURCE
    assert "--no-sandbox" not in BROWSER_MANAGER_SOURCE
