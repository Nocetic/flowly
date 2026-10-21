"""Browser manager survives closing a window without untracked launches."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flowly.desktop_runtime.browser_lifecycle import BROWSER_MANAGER_SOURCE

pytestmark = pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="application-account process integration",
)


def eventually(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    pytest.fail("browser lifecycle did not settle")


def test_close_reopen_and_fixed_requests(tmp_path):
    # Keep the socket path short on macOS too.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fl-browser-", dir="/tmp") as temporary:
        runtime = Path(temporary)
        profile = tmp_path / "profile with spaces"
        profile.mkdir()
        browser = tmp_path / "fake-browser"
        browser.write_text(f"#!{sys.executable}\n" + """
import os, pathlib, signal, sys, time
profile = pathlib.Path(next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--user-data-dir=')))
with (profile / 'launches').open('a') as f: f.write(str(os.getpid()) + '\\n')
(profile / 'arguments').write_text('\\n'.join(sys.argv[1:]))
(profile / 'DevToolsActivePort').write_text('12345\\n/devtools/browser/test')
while not (profile / 'close').exists(): time.sleep(.02)
(profile / 'close').unlink()
""")
        browser.chmod(0o700)
        environment = {"PATH": os.defpath, "XDG_RUNTIME_DIR": str(runtime)}
        manager = subprocess.Popen([sys.executable, "-I", "-c", BROWSER_MANAGER_SOURCE,
                                    str(browser), str(profile)], env=environment,
                                   start_new_session=True)
        try:
            port = profile / "DevToolsActivePort"
            launches = profile / "launches"
            eventually(port.exists)
            first = int(launches.read_text().strip())
            assert os.getpgid(first) == manager.pid
            # Sites refuse sign-in when the page reports automation.
            assert "--disable-blink-features=AutomationControlled" in (profile / "arguments").read_text().split("\n")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                client.connect(str(runtime / "browser-open.sock"))
                for _ in range(8):
                    client.send(b"open")
                (profile / "close").touch()
                eventually(lambda: not port.exists())
                assert manager.poll() is None
                client.send(b"open-malicious")
                client.send(b"/bin/sh")
                time.sleep(0.15)
                assert len(launches.read_text().splitlines()) == 1
                client.send(b"open")
                eventually(port.exists)
                pids = launches.read_text().splitlines()
                assert len(pids) == 2
                assert int(pids[-1]) != first
                assert os.getpgid(int(pids[-1])) == manager.pid
            manager.terminate()
            assert manager.wait(timeout=5) == 0
            assert not port.exists()
            assert not (runtime / "browser-open.sock").exists()
        finally:
            if manager.poll() is None:
                os.killpg(manager.pid, signal.SIGKILL)
                manager.wait(timeout=5)
