"""Detached Linux runtime worker. Only launcher.py may create its reservation."""

from __future__ import annotations

import asyncio
import os
import secrets
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.input_service import InputService
from flowly.desktop_runtime.launcher import (
    ScreenRuntime,
    private_directory,
    process_identity,
    write_state,
)
from flowly.desktop_runtime.observers import ObserverRegistry
from flowly.desktop_runtime.tickets import StreamTickets
from flowly.desktop_runtime.viewers import ViewerCredentials


def write_xauthority(path: Path) -> None:
    # FamilyWild lets Xvnc allocate the display atomically through -displayfd.
    # The cookie is private to this runtime and X11 TCP is disabled.
    data = struct.pack("!H", 65535)
    for field in (b"", b"", b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16)):
        data += struct.pack("!H", len(field)) + field
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)


async def close_browser(port: int) -> None:
    """Ask Chromium to flush its profile before terminating the X session."""
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4), trust_env=False) as client:
        async with client.get(f"http://127.0.0.1:{port}/json/version", allow_redirects=False) as response:
            response.raise_for_status()
            info = await response.json()
        address = info["webSocketDebuggerUrl"]
        parsed = urlsplit(address)
        if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != port
                or not parsed.path.startswith("/devtools/browser/")
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ScreenError("RUNTIME_STATE_INVALID")
        async with client.ws_connect(address) as ws:
            await ws.send_json({"id": 1, "method": "Browser.close"})
            await ws.receive()


def run(directory: Path, generation: str) -> None:
    # A daemon parent may ignore SIGCHLD. Restore normal zombie/reap semantics
    # before spawning: group cleanup relies on retaining each child PID.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    runtime = ScreenRuntime(directory)
    with FileLock(directory / "supervisor.lock", timeout=0, mode=0o600):
        state = runtime.read_manifest()
        if not state or state["generation"] != generation or state["phase"] != "starting":
            raise ScreenError("RUNTIME_STATE_INVALID")
        state["supervisor"] = process_identity(os.getpid())
        write_state(runtime.manifest, state)
        processes: list[subprocess.Popen] = []
        input_service = None
        observers = None
        stopping = False

        def request_stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        def spawn(arguments, env, **kwargs):
            child = subprocess.Popen(arguments, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True, **kwargs)
            processes.append(child)
            identity = process_identity(child.pid)
            if identity is None:
                raise ScreenError("RUNTIME_START_FAILED")
            state["children"].append(identity)
            write_state(runtime.manifest, state)
            return child

        def wait_for(predicate, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if stopping or any(process_identity(child.pid) is None for child in processes):
                    raise ScreenError("RUNTIME_FAILED")
                result = predicate()
                if result:
                    return result
                time.sleep(0.05)
            raise ScreenError("RUNTIME_START_TIMEOUT")

        try:
            sockets = runtime.socket_directory
            xauthority = sockets / "Xauthority"
            write_xauthority(xauthority)
            env = {**os.environ, "XAUTHORITY": str(xauthority), "XDG_RUNTIME_DIR": str(sockets),
                   "XDG_CONFIG_HOME": str(private_directory(directory / "config")),
                   "XDG_CACHE_HOME": str(private_directory(directory / "cache"))}
            rfb = sockets / "rfb.sock"
            rfb.unlink(missing_ok=True)
            read_fd, write_fd = os.pipe()
            try:
                spawn(["Xvnc", "-displayfd", str(write_fd), "-geometry", "1440x900", "-depth", "24",
                       "-auth", str(xauthority), "-nolisten", "tcp", "-rfbport", "-1",
                       "-rfbunixpath", str(rfb), "-rfbunixmode", "0600", "-SecurityTypes", "None",
                       "-AlwaysShared", "-FrameRate", "30"], env, pass_fds=(write_fd,))
                os.close(write_fd)
                write_fd = -1
                wait_for(lambda: select.select([read_fd], [], [], 0)[0])
                raw_display = os.read(read_fd, 32).strip()
                if not raw_display.isdigit():
                    raise ScreenError("RUNTIME_START_FAILED")
                env["DISPLAY"] = ":" + raw_display.decode("ascii")
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)
            spawn(["openbox"], env)
            browser_profile = private_directory(directory / "browser")
            port_file = browser_profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            spawn(["chromium", f"--user-data-dir={browser_profile}", "--remote-debugging-address=127.0.0.1",
                   "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                   "--window-position=0,0", "--window-size=1440,900", "about:blank"], env)
            wait_for(lambda: port_file.exists() and port_file.stat().st_size > 0 and rfb.exists())
            port = int(port_file.read_text().splitlines()[0])
            if not 0 < port < 65536:
                raise ScreenError("RUNTIME_START_FAILED")
            runtime.authority.start(generation)
            (sockets / "input.sock").unlink(missing_ok=True)
            (sockets / "observers.sock").unlink(missing_ok=True)
            viewers = ViewerCredentials(StreamTickets(directory / "admission"))
            observers = ObserverRegistry(runtime.authority, generation, sockets, viewers=viewers)
            input_service = InputService(runtime.authority, generation, sockets, observers=observers, viewers=viewers)
            state.update(phase="running", display=env["DISPLAY"], socket=str(rfb),
                         cdpPort=port, inputServiceVersion=3, observerServiceVersion=3, error=None)
            write_state(runtime.manifest, state)
            while not stopping:
                if (not input_service.is_alive or not observers.is_alive
                        or any(process_identity(child.pid) is None for child in processes)):
                    raise ScreenError("RUNTIME_FAILED")
                time.sleep(0.2)
            state.update(phase="stopped", error=None)
        except BaseException:
            # Never persist browser stderr, keys, URLs or arbitrary exception
            # text. Health failure does not release durable human authority.
            state.update(phase="failed", error="RUNTIME_FAILED")
        finally:
            if observers is not None:
                try:
                    observers.close()
                except Exception:
                    state.update(phase="failed", error="OBSERVER_SERVICE_CLOSE_FAILED")
            if input_service is not None:
                try:
                    input_service.close()
                except Exception:
                    state.update(phase="failed", error="INPUT_SERVICE_CLOSE_FAILED")
            if state["phase"] == "stopped" and state.get("cdpPort"):
                try:
                    asyncio.run(close_browser(state["cdpPort"]))
                    # Keep the child unreaped until final process-group cleanup.
                    deadline = time.monotonic() + 5
                    while process_identity(processes[-1].pid) is not None:
                        if time.monotonic() >= deadline:
                            raise ScreenError("RUNTIME_STOP_TIMEOUT")
                        time.sleep(0.05)
                except Exception:
                    state.update(phase="failed", error="BROWSER_CLOSE_FAILED")
            for child in reversed(processes):
                # These are our unreaped children, not persisted numeric PIDs.
                # Even an exited leader remains unreaped until its group is
                # signalled; poll() here would permit PID reuse too early.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=2)
            write_state(runtime.manifest, state)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    run(Path(sys.argv[1]), sys.argv[2])
