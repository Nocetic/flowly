"""Application-account browser lifecycle payload for the virtual desktop.

The manager stays in the supervisor's tracked process group while Chromium
windows may close and reopen. Its private socket accepts one fixed operation;
it cannot pass commands, URLs, profiles, or environment values from a client.
"""

BROWSER_MANAGER_SOURCE = r'''
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

if os.geteuid() == 0:
    raise SystemExit("browser manager requires the application account")
browser, profile, *fixed = sys.argv[1:]
if not Path(browser).is_absolute() or not Path(profile).is_absolute():
    raise ValueError("application paths must be absolute")
# A root host's supervisor picks the DevTools port and guards it before launch.
debugging_port = int(fixed[0]) if fixed else 0
if len(fixed) > 1 or not 0 <= debugging_port < 65536:
    raise ValueError("invalid DevTools port")
runtime = Path(os.environ["XDG_RUNTIME_DIR"])
info = runtime.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
    raise ValueError("invalid application runtime")
address = runtime / "browser-open.sock"
port = Path(profile) / "DevToolsActivePort"
# Refuse an occupied/stale path. Only the owning runtime may prepare a new
# generation, and two managers must never compete for one browser profile.
server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
server.bind(str(address))
os.chmod(address, 0o600)
server.settimeout(0.1)
stopping = False
child = None
pending = True
next_launch = 0.0

def stop(*_):
    global stopping
    stopping = True


def fresh_start():
    """Start with one tab, never the last session's. Some builds (Chrome for
    Testing on a server) continue where they left off: every start brought
    back yesterday's tabs plus one more blank one. Cookies and logins live in
    other files and stay."""
    default = Path(profile) / "Default"
    if default.is_symlink() or not default.is_dir():
        return
    preferences = default / "Preferences"
    try:
        data = json.loads(preferences.read_text()) if preferences.is_file() and not preferences.is_symlink() else None
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        session["restore_on_startup"] = 5  # open the new-tab page
        session.pop("startup_urls", None)
        data["session"] = session
        recorded = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        recorded.update(exit_type="Normal", exited_cleanly=True)  # no "restore pages?" either
        data["profile"] = recorded
        staged = default / "Preferences.flowly-start"
        try:
            staged.write_text(json.dumps(data))
            os.replace(staged, preferences)
        except OSError:
            staged.unlink(missing_ok=True)
    stores = [default / "Sessions", *(default / name for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"))]
    for store in stores:
        if store.is_symlink():
            continue
        if store.is_dir():
            for entry in store.iterdir():
                if entry.is_file() and not entry.is_symlink():
                    entry.unlink(missing_ok=True)
        elif store.is_file():
            store.unlink(missing_ok=True)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGCHLD, signal.SIG_DFL)
try:
    while not stopping:
        if child is not None and child.poll() is not None:
            child = None
            port.unlink(missing_ok=True)
        if pending and child is None and time.monotonic() >= next_launch:
            pending = False
            next_launch = time.monotonic() + 1
            port.unlink(missing_ok=True)
            try:
                fresh_start()
            except OSError:
                pass  # never keep the browser from starting
            try:
                # Inherit the manager's process group. The supervisor retains
                # that group's leader until final cleanup, even after a browser
                # exits; no client-provided PID is ever signalled.
                child = subprocess.Popen([
                    browser, "--user-data-dir=" + profile,
                    # One window class for every build: the dock finds the
                    # running browser by it.
                    "--class=chromium",
                    # The pinned Chrome for Testing build would otherwise show
                    # a "for automated testing" bar to the person in control.
                    *(["--test-type"] if browser.startswith("/opt/flowly-screen/chromium/") else []),
                    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(debugging_port),
                    "--no-first-run", "--no-default-browser-check",
                    # Bounded HTTP cache; cookies and logins are not cache.
                    "--disk-cache-size=268435456",
                    "--window-position=20,20", "--window-size=1360,780", "about:blank",
                ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
            except OSError:
                pass
        try:
            request = server.recv(5)
        except socket.timeout:
            continue
        # recv(5) ensures an overlong datagram cannot match the four-byte verb.
        if request == b"open" and child is None:
            pending = True
finally:
    server.close()
    address.unlink(missing_ok=True)
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    port.unlink(missing_ok=True)
'''

BROWSER_OPEN_SOURCE = r'''
import os
import socket
from pathlib import Path
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
    client.settimeout(1)
    client.connect(str(Path(os.environ["XDG_RUNTIME_DIR"]) / "browser-open.sock"))
    client.send(b"open")
'''
