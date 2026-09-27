"""Application-account browser lifecycle payload for the virtual desktop.

The manager stays in the supervisor's tracked process group while Chromium
windows may close and reopen. Its private socket accepts one fixed operation;
it cannot pass commands, URLs, profiles, or environment values from a client.
"""

BROWSER_MANAGER_SOURCE = r'''
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
browser, profile = sys.argv[1:]
if not Path(browser).is_absolute() or not Path(profile).is_absolute():
    raise ValueError("application paths must be absolute")
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
                # Inherit the manager's process group. The supervisor retains
                # that group's leader until final cleanup, even after a browser
                # exits; no client-provided PID is ever signalled.
                child = subprocess.Popen([
                    browser, "--user-data-dir=" + profile,
                    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
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
