"""Which process a subagent run belongs to, and whether that process is gone.

Every running run records its owner: the process (pid and its start
identity, so a reused pid is not mistaken for it), the machine's boot (so a
reboot ends every owner at once), the host name (for people reading the
index) and the manager instance inside that process. Only the owner works
on a run. Another process may settle or continue it only when the owner is
provably dead — a CLI or a second gateway on the same Flowly home must never
finish a live gateway's work as "restarted".

A Flowly home lives on one machine: an owner recorded under another boot is
treated as gone, because the host name alone cannot tell "another machine"
from "this machine after a reboot" (macOS renames hosts with the network).
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

# Owners that are not a SubagentManager (a delegated task) use this one.
PROCESS_INSTANCE = uuid.uuid4().hex

# Manager instances alive in this process. An owner whose pid is ours but
# whose instance is not here belonged to an earlier process with our pid.
_LIVE_INSTANCES: set[str] = {PROCESS_INSTANCE}
_cache_lock = threading.Lock()
_cached: dict[str, Any] = {}

OURS, ALIVE, DEAD = "ours", "alive", "dead"


def register_instance(instance: str) -> None:
    _LIVE_INSTANCES.add(instance)


def forget_instance(instance: str) -> None:
    """The instance stopped owning work (its manager is gone)."""
    if instance != PROCESS_INSTANCE:
        _LIVE_INSTANCES.discard(instance)


def boot_id() -> str | None:
    """This boot of the machine; None when the platform does not say."""
    with _cache_lock:
        if "boot" not in _cached:
            _cached["boot"] = _read_boot_id()
        return _cached["boot"]


def _read_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        if value:
            return f"linux:{value}"
    except OSError:
        pass
    if platform.system() == "Darwin":
        for key in ("kern.bootsessionuuid", "kern.boottime"):
            try:
                value = subprocess.check_output(
                    ["sysctl", "-n", key], text=True, stderr=subprocess.DEVNULL, timeout=2,
                ).strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if value:
                return f"darwin:{value}"
    return None


def _own_identity() -> str | None:
    with _cache_lock:
        if "identity" not in _cached or _cached.get("pid") != os.getpid():
            from flowly.profile import _process_identity

            _cached["pid"] = os.getpid()
            _cached["identity"] = _process_identity(os.getpid())
        return _cached["identity"]


def process_owner(instance: str = PROCESS_INSTANCE) -> dict[str, Any]:
    """The owner stamp for work this process (and ``instance``) does."""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    return {
        "instance": instance,
        "pid": os.getpid(),
        "identity": _own_identity(),
        "boot": boot_id(),
        "host": host,
    }


def owner_state(owner: Any, instance: str | None = None) -> str:
    """OURS, ALIVE or DEAD for a recorded owner.

    A run with no owner (recorded before owners existed) has nobody to wait
    for: DEAD. Anything that cannot be proven gone counts as ALIVE.
    """
    if not isinstance(owner, dict) or type(owner.get("pid")) is not int:
        return DEAD
    if instance is not None and owner.get("instance") == instance:
        return OURS
    recorded_boot, current_boot = owner.get("boot"), boot_id()
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return DEAD
    pid = owner["pid"]
    recorded_identity = owner.get("identity")
    if pid == os.getpid():
        current = _own_identity()
        if recorded_identity and current and recorded_identity != current:
            return DEAD
        return ALIVE if owner.get("instance") in _LIVE_INSTANCES else DEAD
    from flowly.profile import _pid_is_alive, _process_identity

    if not _pid_is_alive(pid):
        return DEAD
    if recorded_identity:
        current = _process_identity(pid)
        if current and current != recorded_identity:
            return DEAD  # the pid now belongs to another process
    return ALIVE
