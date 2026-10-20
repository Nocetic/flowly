"""Read-only Linux service ownership checks for gateway-initiated restarts."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from flowly.desktop_runtime.handoff import process_identity

_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "ControlGroup", "InvocationID")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")


def _process_cgroups(pid: int) -> set[str]:
    """Only the unified hierarchy or legacy systemd controller owns a service."""
    result = set()
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        hierarchy, controllers, group = line.split(":", 2)
        if (hierarchy == "0" and not controllers) or "name=systemd" in controllers.split(","):
            if group.startswith("/") and ".." not in group.split("/"):
                result.add(group)
    return result


async def _show(unit: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "show", unit, "--no-pager",
        "--property=" + ",".join(_PROPERTIES),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 3)
        if proc.returncode != 0 or len(stdout) > 16384:
            return None
        return stdout.decode("utf-8", "strict")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def owns_current_gateway(label: str, expected_pid: int) -> bool:
    """Prove this process is the exact main process of the selected user unit.

    Child profile gateways must not restart their host's service. A missing or
    ambiguous observation requires a manual restart. This is admission evidence,
    not an atomic systemd compare-and-restart transaction or a work-idleness test.
    """
    if type(expected_pid) is not int or expected_pid != os.getpid() or not isinstance(label, str) or not _LABEL.fullmatch(label):
        return False
    unit = label if label.endswith(".service") else label + ".service"
    try:
        before = process_identity(expected_pid)
        if before is None:
            return False
        groups_before = _process_cgroups(expected_pid)
        output = await _show(unit)
        if output is None:
            return False
        values = {}
        for line in output.splitlines():
            key, value = line.split("=", 1)
            if key in values or key not in _PROPERTIES:
                return False
            values[key] = value
        if set(values) != set(_PROPERTIES):
            return False
        return (
            values["Id"] == unit
            and values["LoadState"] == "loaded"
            and values["ActiveState"] == "active"
            and values["SubState"] == "running"
            and values["MainPID"] == str(expected_pid)
            and re.fullmatch(r"[0-9a-f]{32}", values["InvocationID"]) is not None
            and values["ControlGroup"] not in {"", "/"}
            and values["ControlGroup"] in groups_before
            and _process_cgroups(expected_pid) == groups_before
            and process_identity(expected_pid) == before
        )
    except (OSError, ValueError, asyncio.TimeoutError):
        return False
