"""A screen's private systemd scope, never the gateway's service cgroup.

On systemd/cgroup-v2 hosts admission is mandatory and verified before launching
applications. Containers without a systemd manager depend on any externally
configured container limit; their manifest records that no private limit exists.
Scope names are derived locally, never accepted from an RPC or persisted path.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError

MEMORY_MAX = 1024**3
MEMORY_HIGH = 768 * 1024**2
SWAP_MAX = 256 * 1024**2
TASKS_MAX = 4096


def scope_name(directory: Path, generation: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ScreenError("RUNTIME_STATE_INVALID")
    profile = hashlib.sha256(os.fsencode(directory.absolute())).hexdigest()[:24]
    return f"flowly-screen-{profile}-{generation}.scope"


def available() -> bool:
    return Path("/run/systemd/system").is_dir() and Path("/sys/fs/cgroup/cgroup.controllers").is_file()


def scoped_command(command: list[str], directory: Path, generation: str) -> tuple[list[str], bool]:
    if not available():
        return command, False
    executable = shutil.which("systemd-run", path="/usr/bin:/bin")
    if not executable:
        raise ScreenError("RUNTIME_START_FAILED")
    # Scope mode inherits the invoking environment, working directory and uid.
    # --collect removes the unit after its last process exits. A failed launch
    # must never be retried without limits.
    return [executable, *(["--user"] if os.geteuid() else []), "--scope", "--quiet", "--collect",
            "--unit=" + scope_name(directory, generation),
            f"--property=MemoryMax={MEMORY_MAX}", f"--property=MemoryHigh={MEMORY_HIGH}",
            f"--property=MemorySwapMax={SWAP_MAX}", f"--property=TasksMax={TASKS_MAX}",
            "--property=TimeoutStopSec=5s",
            "--", *command], True


def verify_scope(directory: Path, generation: str, *, proc: Path = Path("/proc"),
                 root: Path = Path("/sys/fs/cgroup")) -> dict:
    """Read back the actual kernel limits; manager acceptance alone is not proof."""
    try:
        records = [line[3:] for line in (proc / "self/cgroup").read_text().splitlines()
                   if line.startswith("0::")]
        if len(records) != 1 or not records[0].startswith("/") or ".." in records[0].split("/"):
            raise ValueError()
        path = root / records[0].lstrip("/")
        if path.name != scope_name(directory, generation):
            raise ValueError()
        limits = {"memory.max": MEMORY_MAX, "memory.high": MEMORY_HIGH,
                  "memory.swap.max": SWAP_MAX, "pids.max": TASKS_MAX}
        observed = {}
        for field, maximum in limits.items():
            actual = (path / field).read_text().strip()
            if not actual.isdecimal() or not 0 <= int(actual) <= maximum or (actual == "0" and field != "memory.swap.max"):
                raise ValueError()
            observed[field] = int(actual)
        return {"enforced": True, "memoryMaxBytes": observed["memory.max"],
                "memoryHighBytes": observed["memory.high"], "swapMaxBytes": observed["memory.swap.max"],
                "tasksMax": observed["pids.max"]}
    except (OSError, ValueError):
        raise ScreenError("RUNTIME_START_FAILED") from None


def stop_scope(directory: Path, generation: str) -> None:
    """Reap daemonized descendants too, including on non-root systemd hosts.

    Only call after the runtime's authority authorizes stop. The randomized,
    profile-bound unit name cannot target the agent or a different generation.
    Stopping an already collected unit is idempotent in systemd.
    """
    executable = shutil.which("systemctl", path="/usr/bin:/bin")
    if not executable:
        raise ScreenError("RUNTIME_STOP_FAILED")
    command = [executable, *(["--user"] if os.geteuid() else []), "stop",
               scope_name(directory, generation)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=12, env={**os.environ, "LC_ALL": "C"})
        if result.returncode and b"not loaded" not in result.stderr:
            raise ScreenError("RUNTIME_STOP_FAILED")
    except (OSError, subprocess.TimeoutExpired):
        raise ScreenError("RUNTIME_STOP_FAILED") from None
