"""Detached Linux runtime worker. Only launcher.py may create its reservation."""

from __future__ import annotations

import asyncio
import os
import re
import resource
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from flowly.desktop_runtime.application_layout import read_browser_port
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.browser_lifecycle import BROWSER_MANAGER_SOURCE
from flowly.desktop_runtime.desktop_defaults import DESKTOP_DEFAULTS_SOURCE
from flowly.desktop_runtime.desktop_keymap import KEYMAP_SOURCE, RemoteKeymap
from flowly.desktop_runtime.devtools_guard import free_port, guard, listener_uid, unguard
from flowly.desktop_runtime.input_service import InputService
from flowly.desktop_runtime.installation_gate import environment_activity
from flowly.desktop_runtime.launcher import (
    ScreenRuntime,
    private_directory,
    process_identity,
    write_state,
)
from flowly.desktop_runtime.observers import ObserverRegistry
from flowly.desktop_runtime.tickets import StreamTickets
from flowly.desktop_runtime.viewers import ViewerCredentials

# Under memory pressure the kernel picks the screen before the agent (whose
# score stays 0). Children inherit it; without CAP_SYS_RESOURCE they cannot
# lower it below the root supervisor's floor.
SCREEN_OOM_SCORE_ADJ = 500
# Tasks (processes and threads) of a profile's dedicated account: a runaway
# terminal job cannot exhaust the host's process table. Chromium with the
# 16-tab limit stays far below it.
ACCOUNT_TASK_LIMIT = 4096


def limit_screen_resources(*, system: bool) -> None:
    Path("/proc/self/oom_score_adj").write_text(str(SCREEN_OOM_SCORE_ADJ))
    if system:
        resource.setrlimit(resource.RLIMIT_NPROC, (ACCOUNT_TASK_LIMIT, ACCOUNT_TASK_LIMIT))


def application_environment(directory: Path, sockets: Path) -> dict[str, str]:
    """Explicit desktop environment; never inherit agent credentials or loaders."""
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(private_directory(directory / "home")),
        "XAUTHORITY": str(sockets / "Xauthority"),
        "XDG_RUNTIME_DIR": str(sockets),
        "XDG_CONFIG_HOME": str(private_directory(directory / "config")),
        "XDG_CACHE_HOME": str(private_directory(directory / "cache")),
        "XDG_DATA_HOME": str(private_directory(directory / "data")),
    }
    # dbus-run-session creates this private runtime's Unix bus. Do not forward
    # arbitrary alternate transports, authentication variables or loader flags.
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        if (len(address) > 1024 or not address.startswith(("unix:path=", "unix:abstract="))
                or any(char in address for char in ";\r\n\x00")):
            raise ScreenError("RUNTIME_STATE_INVALID")
        environment["DBUS_SESSION_BUS_ADDRESS"] = address
    return environment


def application_command(arguments: list[str], *, system: bool = False) -> list[str]:
    # Preserve explicitly configured executable locations without passing the
    # agent's entire PATH to the desktop processes or their descendants.
    executable = shutil.which(arguments[0], path="/usr/bin:/bin" if system else None)
    if executable is None:
        raise ScreenError("DEPENDENCIES_MISSING")
    return [os.path.abspath(executable), *arguments[1:]]


def write_xauthority(path: Path, *, account=None) -> None:
    # FamilyWild lets Xvnc allocate the display atomically through -displayfd.
    # The cookie is private to this runtime and X11 TCP is disabled.
    data = struct.pack("!H", 65535)
    for field in (b"", b"", b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16)):
        data += struct.pack("!H", len(field)) + field
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        if account is not None:
            os.fchown(target.fileno(), account.uid, account.gid)
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
    runtime = ScreenRuntime(directory, create=False)
    with environment_activity(), FileLock(directory / "supervisor.lock", timeout=0, mode=0o600):
        state = runtime.read_manifest()
        if not state or state["generation"] != generation or state["phase"] != "starting":
            raise ScreenError("RUNTIME_STATE_INVALID")
        state["supervisor"] = process_identity(os.getpid())
        write_state(runtime.manifest, state)
        processes: list[subprocess.Popen] = []
        input_service = None
        observers = None
        stopping = False
        application = None
        guarded = None
        devtools_port = None

        def request_stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        def privileges():
            return ({"user": application.account.uid, "group": application.account.gid,
                     "extra_groups": [], "umask": 0o077} if application else {})

        def spawn(arguments, env, **kwargs):
            child = subprocess.Popen(application_command(arguments, system=application is not None), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True, cwd=env["HOME"], **privileges(), **kwargs)
            processes.append(child)
            identity = process_identity(child.pid)
            if identity is None:
                raise ScreenError("RUNTIME_START_FAILED")
            state["children"].append(identity)
            write_state(runtime.manifest, state)
            return child

        def application_check(arguments, env, *, timeout=5):
            # Short helpers cannot inherit root credentials or outlive a timeout.
            # In particular, seeding must not write app-owned paths as root.
            child = subprocess.Popen(application_command(arguments, system=application is not None), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True,
                                     close_fds=True, cwd=env["HOME"], **privileges())
            try:
                return child.wait(timeout=timeout) == 0
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
                raise

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
            if os.geteuid() == 0:
                from flowly.desktop_runtime.application_layout import root_application_layout

                if state.get("socketLayoutVersion") != 3:
                    raise ScreenError("RUNTIME_STATE_INVALID")
                application = root_application_layout(directory)
            limit_screen_resources(system=application is not None)
            sockets = runtime.socket_directory
            rfb = runtime.rfb_socket_for(state)
            application_sockets = rfb.parent
            xauthority = application_sockets / "Xauthority"
            write_xauthority(xauthority, account=application.account if application else None)
            env = application.environment() if application else application_environment(directory, application_sockets)
            env.update(XDG_CURRENT_DESKTOP="XFCE", XDG_SESSION_TYPE="x11", SHELL="/bin/bash")
            if application:
                # The root supervisor keeps its own control sockets. Only this
                # private unprivileged bus is forwarded to the applications.
                bus = application_sockets / "bus"
                bus.unlink(missing_ok=True)
                read_fd, write_fd = os.pipe()
                try:
                    address = "unix:path=" + str(bus)
                    spawn(["dbus-daemon", "--session", "--nofork", "--nopidfile",
                           "--address=" + address, "--print-address=" + str(write_fd)], env, pass_fds=(write_fd,))
                    os.close(write_fd)
                    write_fd = -1
                    wait_for(lambda: select.select([read_fd], [], [], 0)[0])
                    actual = os.read(read_fd, 1024).strip().decode("ascii")
                    if re.fullmatch(re.escape(address) + r",guid=[a-f0-9]{32}", actual) is None:
                        raise ScreenError("RUNTIME_START_FAILED")
                    env["DBUS_SESSION_BUS_ADDRESS"] = actual
                finally:
                    os.close(read_fd)
                    if write_fd >= 0:
                        os.close(write_fd)
            rfb.unlink(missing_ok=True)
            read_fd, write_fd = os.pipe()
            try:
                spawn(["Xvnc", "-displayfd", str(write_fd), "-geometry", "1440x900", "-depth", "24",
                       "-auth", str(xauthority), "-nolisten", "tcp", "-rfbport", "-1",
                       "-rfbunixpath", str(rfb), "-rfbunixmode", "0600", "-SecurityTypes", "None",
                       "-AlwaysShared", "-FrameRate", "30",
                       # What the person in control or the agent copies may be a
                       # password: never push it to viewers. Paste keeps working
                       # (typed), and client cut text matches the gate's bound.
                       "-SendCutText=0", "-MaxCutText", "65536"], env, pass_fds=(write_fd,))
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
            browser_profile = application.browser if application else private_directory(directory / "browser")
            from flowly.desktop_runtime.install_recipe import screen_browser

            browser_command = screen_browser()
            if browser_command is None:
                raise ScreenError("DEPENDENCIES_MISSING")
            if not application_check(["python3", "-I", "-c", DESKTOP_DEFAULTS_SOURCE,
                                      browser_command, str(browser_profile)], env):
                raise ScreenError("RUNTIME_START_FAILED")
            spawn(["xfwm4", "--compositor=on", "--vblank=off"], env)
            spawn(["xfsettingsd"], env)
            spawn(["xfdesktop"], env)
            spawn(["xfce4-panel"], env)
            wait_for(lambda: all(application_check(
                ["xdotool", "search", "--onlyvisible", "--class", pattern], env)
                for pattern in ("^xfce4-panel$", "^xfdesktop$")))
            # Before the browser or a terminal reads the keymap: layout
            # characters become ordinary keys (see desktop_keymap). Without it
            # ASCII still works; the manifest records which keymap is active.
            def upload_keymap(extras):
                try:
                    return application_check(["python3", "-I", "-c", KEYMAP_SOURCE, application_command(
                        ["xkbcomp"], system=application is not None)[0], *("%x" % value for value in extras)],
                        env, timeout=15)
                except (ScreenError, subprocess.TimeoutExpired):
                    return False

            seeded = upload_keymap([])
            keymap = RemoteKeymap(upload_keymap) if seeded else None
            state["keymap"] = "seeded" if seeded else "default"
            port_file = browser_profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            (application_sockets / "browser-open.sock").unlink(missing_ok=True)
            fixed_port = []
            if application:
                # Only root (the agent's gateway) may reach this profile's
                # DevTools: another profile's account must not drive its browser.
                devtools_port = free_port()
                guard(application.directory.name, devtools_port)
                guarded = application.directory.name
                fixed_port = [str(devtools_port)]
            spawn(["python3", "-I", "-c", BROWSER_MANAGER_SOURCE, browser_command, str(browser_profile),
                   *fixed_port], env)

            def browser_port():
                if application:
                    # Chromium records DevToolsActivePort only for an ephemeral
                    # port. The fixed port is ready once this profile's own
                    # account listens on it, never a process that took it.
                    return devtools_port if listener_uid(devtools_port) == application.account.uid else None
                return read_browser_port(browser_profile, os.geteuid())

            port = wait_for(lambda: browser_port() if rfb.exists() else None)
            if not 0 < port < 65536:
                raise ScreenError("RUNTIME_START_FAILED")
            runtime.authority.start(generation)
            (sockets / "input.sock").unlink(missing_ok=True)
            (sockets / "observers.sock").unlink(missing_ok=True)
            viewers = ViewerCredentials(StreamTickets(directory / "admission"))
            observers = ObserverRegistry(runtime.authority, generation, sockets, viewers=viewers)
            input_service = InputService(runtime.authority, generation, sockets, observers=observers, viewers=viewers,
                                         rfb_socket=rfb, keymap=keymap)
            state.update(phase="running", display=env["DISPLAY"], socket=str(rfb),
                         cdpPort=port, inputServiceVersion=3, observerServiceVersion=3, error=None)
            write_state(runtime.manifest, state)
            while not stopping:
                if (not input_service.is_alive or not observers.is_alive
                        or any(process_identity(child.pid) is None for child in processes)):
                    raise ScreenError("RUNTIME_FAILED")
                current_port = browser_port()
                if state.get("cdpPort") != current_port:
                    state["cdpPort"] = current_port
                    write_state(runtime.manifest, state)
                time.sleep(0.2)
            state.update(phase="stopped", error=None)
        except BaseException as exc:
            # Never persist browser stderr, keys, URLs or arbitrary exception
            # text. Health failure does not release durable human authority.
            isolation = isinstance(exc, ScreenError) and exc.code == "SCREEN_ISOLATION_UNAVAILABLE"
            state.update(phase="failed", error="SCREEN_ISOLATION_UNAVAILABLE" if isolation else "RUNTIME_FAILED")
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
                    # Manager stays alive; wait for it to observe browser exit
                    # and remove the port record before terminating its group.
                    deadline = time.monotonic() + 5
                    while browser_port() is not None:
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
            if guarded is not None:
                # The browser is gone; a later start replaces the rule anyway.
                unguard(guarded)
            write_state(runtime.manifest, state)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    run(Path(sys.argv[1]), sys.argv[2])
