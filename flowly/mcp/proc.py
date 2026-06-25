"""Best-effort orphan reaping for stdio MCP subprocesses (Faz 2c, S7).

The MCP SDK spawns stdio servers as child processes and tears them down
when its transport context exits. On Linux, a server that calls
``setsid()`` escapes the parent's process group, so if the owning task
is *cancelled* mid-flight the child can survive as an orphan.

This module snapshots our direct children around the spawn and, on
teardown, force-terminates only those that (a) first appeared during
this server's spawn window and (b) are still alive. Everything is
strictly best-effort and exception-safe — we never raise into the
transport teardown, and we never touch a PID that wasn't our own fresh
child. No-op on Windows and when ``ps`` is unavailable.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time


logger = logging.getLogger(__name__)


def _supported() -> bool:
    return sys.platform != "win32"


def snapshot_child_pids() -> set[int]:
    """Return the set of PIDs whose parent is this process. Empty on failure."""
    if not _supported():
        return set()
    our_pid = os.getpid()
    try:
        # ``-A`` lists every process (not just the controlling terminal's,
        # which is empty under a test runner / daemon with no TTY). Works
        # on both Linux and macOS.
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()

    children: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if ppid == our_pid:
            children.add(pid)
    return children


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not ours to signal — treat as alive but untouchable.
        return True
    except OSError:
        return False


def reap_pids(pids: set[int], server_name: str = "") -> None:
    """Force-terminate the given PIDs if still alive. Best-effort, never raises.

    SIGTERM first, then SIGKILL after a short grace period.
    """
    if not _supported() or not pids:
        return
    survivors = {pid for pid in pids if _alive(pid)}
    if not survivors:
        return

    for pid in survivors:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Brief grace period for graceful exit.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        survivors = {pid for pid in survivors if _alive(pid)}
        if not survivors:
            break
        time.sleep(0.1)

    # SIGKILL is POSIX-only; on Windows it doesn't exist (AttributeError) and
    # os.kill() maps SIGTERM to TerminateProcess, which is the hard kill there.
    _kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in survivors:
        try:
            os.kill(pid, _kill_sig)
            logger.debug(
                "MCP server '%s': reaped orphan stdio child pid %d",
                server_name or "?", pid,
            )
        except OSError:
            pass
