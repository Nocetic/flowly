"""Whether a local TCP port is held by another user of this computer.

The gateway port is one per computer, not per user. On a Mac with two people
signed in, the first agent to start holds 18790 and the second one failed to
bind, forever, while launchd restarted it (2026-11-09, a second macOS user).
An agent that finds its port held by another user moves to a free one and
keeps it; one held by this user is left to the usual handover.

Never raises: an owner that cannot be read counts as "not another user", and
callers then behave exactly as before this existed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys


def linux_listener_uids(proc_net_tcp: str, port: int) -> list[int]:
    """The uids that listen on ``port`` in ``/proc/net/tcp`` (or ``tcp6``) text."""
    uids: list[int] = []
    for line in proc_net_tcp.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[3] != "0A":  # 0A = LISTEN
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
            uid = int(fields[7])
        except (IndexError, ValueError):
            continue
        if local_port == port and uid not in uids:
            uids.append(uid)
    return uids


def _own_listener_pids(port: int) -> list[int] | None:
    """This user's processes listening on ``port`` (``lsof`` shows no one else's); None if unreadable."""
    try:
        done = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode not in (0, 1):  # 1: nothing found
        return None
    return [int(pid) for pid in done.stdout.split() if pid.isdigit()]


def _answers(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def held_by_another_user(port: int) -> bool:
    """True only when the port is provably held by a process that is not this user's.

    macOS: ``lsof`` lists only this user's sockets (``netstat`` run from an
    unsigned interpreter comes back empty there), so a port that answers with
    none of this user's processes listening is someone else's. Linux:
    ``/proc/net/tcp`` names the listener's uid. Elsewhere nothing moves.
    """
    if not hasattr(os, "getuid"):
        return False
    if sys.platform == "darwin":
        own = _own_listener_pids(port)
        return own is not None and not own and _answers(port)
    if sys.platform.startswith("linux"):
        found: list[int] = []
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(name, encoding="ascii") as table:
                    found += linux_listener_uids(table.read(), port)
            except OSError:
                continue
        return bool(found) and os.getuid() not in found
    return False


def free_port(near: int, span: int = 20) -> int:
    """A port no one listens on: the next few after ``near`` first, else one the system picks."""
    for candidate in range(near + 1, near + 1 + span):
        if _bindable(candidate):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    # Another user's wildcard listener does not always stop a loopback bind.
    return not _answers(port)


def move_off_another_users_port(port: int, persist) -> int:
    """``port``, or a free one (handed to ``persist``) when another user holds it."""
    if not held_by_another_user(port):
        return port
    moved = free_port(port)
    persist(moved)
    return moved
