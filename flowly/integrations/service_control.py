"""Restart the local Flowly gateway service from inside the TUI.

When config mutations need a full gateway restart (channel + tool + voice
changes attach to the bus at boot — hot-reload alone can't swap them),
the TUI can ask the OS service manager to bounce the gateway and the
running TUI session reconnects on its own.

Three platforms supported, in this priority:
1. **macOS launchd** — ``launchctl kickstart -k`` does atomic stop+start
   on the user-domain service (``gui/<uid>/<label>``).
2. **Linux systemd-user** — ``systemctl --user restart <label>``.
3. **Windows** — Task Scheduler task ``/end`` + ``/run`` via ``schtasks``
   (the gateway is a scheduled task, not a Windows Service).

If the service isn't installed (user runs gateway manually in another
terminal), the helper returns a soft failure so the modal can show
"please restart gateway terminal" instead of crashing.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shlex
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "ai.flowly.gateway"


def _unit_exec_path(label: str) -> Path | None:
    """The executable the installed service unit points at, if parseable.

    Reads the systemd user unit's ``ExecStart`` (Linux) or the launchd plist's
    ``ProgramArguments[0]`` (macOS). Returns ``None`` when the unit doesn't
    exist or can't be parsed — callers treat that as "nothing to say".
    """
    system = platform.system().lower()
    try:
        if system == "linux":
            unit = Path.home() / ".config" / "systemd" / "user" / f"{label}.service"
            if not unit.is_file():
                return None
            for line in unit.read_text(encoding="utf-8").splitlines():
                if line.startswith("ExecStart="):
                    tokens = shlex.split(line.split("=", 1)[1].strip())
                    return Path(tokens[0]) if tokens else None
        elif system == "darwin":
            import plistlib

            plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if not plist.is_file():
                return None
            with plist.open("rb") as f:
                args = plistlib.load(f).get("ProgramArguments") or []
            return Path(args[0]) if args else None
    except Exception:
        return None
    return None


def _stale_exec_hint(label: str) -> str:
    """A pointed hint when the unit's executable no longer exists on disk.

    The classic way to get here: the service was installed by a previous
    (e.g. PyPI) install whose binary a later install retired — the service
    manager keeps reporting ok while the gateway never binds its port.
    Empty string when the executable is fine (or unknowable).
    """
    exec_path = _unit_exec_path(label)
    if exec_path is None or exec_path.exists():
        return ""
    return (
        f" — the service points at {exec_path}, which no longer exists "
        f"(a previous install?). Fix:  flowly service install --start"
    )


@dataclass
class RestartResult:
    ok: bool
    method: str        # "launchctl" | "systemctl" | "sc" | "no_service" | "error"
    detail: str        # human-readable message
    paused_seconds: float = 0.0  # how long we waited for the gateway to come back up


async def restart_gateway(
    label: str = DEFAULT_LABEL,
    *,
    health_check_host: str = "127.0.0.1",
    health_check_port: int = 18790,
    health_check_timeout: float = 10.0,
) -> RestartResult:
    """Bounce the gateway service if installed; otherwise return a soft no-op.

    Returns ``RestartResult.ok = False`` (with ``method='no_service'``)
    when the service isn't installed — caller surfaces this as a hint to
    restart the manually-launched gateway terminal. ``method='error'``
    means the restart command itself failed.

    After kicking the service we poll the gateway's TCP socket until it
    comes back up (or ``health_check_timeout`` elapses) so the caller
    can guarantee subsequent requests hit the new process.
    """
    system = platform.system().lower()
    started = asyncio.get_event_loop().time()

    if system == "darwin":
        # First check if the service is actually installed/loaded.
        installed = await _launchctl_loaded(label)
        if not installed:
            return RestartResult(
                ok=False, method="no_service",
                detail=(
                    f"launchd service '{label}' isn't loaded — "
                    f"gateway is probably running manually. Quit the gateway "
                    f"terminal and run `flowly gateway` again."
                ),
            )
        # kickstart -k = SIGTERM the running job + start a fresh one. Atomic;
        # avoids the "stop, then load, then start" race in our older code.
        uid = os.getuid()
        cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
        rc, out, err = await _run(cmd)
        if rc != 0:
            return RestartResult(
                ok=False, method="error",
                detail=f"launchctl kickstart failed: {(err or out).strip()}",
            )
        # Wait for the new process to bind its port — otherwise the next
        # WS reconnect would race the relaunch and fail.
        came_back = await _wait_for_port(
            health_check_host, health_check_port, health_check_timeout,
        )
        elapsed = asyncio.get_event_loop().time() - started
        if not came_back:
            return RestartResult(
                ok=False, method="launchctl",
                detail=(
                    f"kickstart sent but gateway didn't bind "
                    f"{health_check_host}:{health_check_port} within "
                    f"{health_check_timeout:.0f}s — check logs"
                    + _stale_exec_hint(label)
                ),
                paused_seconds=elapsed,
            )
        return RestartResult(
            ok=True, method="launchctl",
            detail=f"restarted via launchd ({elapsed:.1f}s downtime)",
            paused_seconds=elapsed,
        )

    if system == "linux":
        if shutil.which("systemctl") is None:
            return RestartResult(
                ok=False, method="no_service",
                detail="systemctl not found — restart gateway manually",
            )
        cmd = ["systemctl", "--user", "restart", label]
        rc, out, err = await _run(cmd)
        if rc != 0:
            err_text = (err or out).strip()
            if "could not find unit" in err_text.lower() or rc == 5:
                return RestartResult(
                    ok=False, method="no_service",
                    detail=(
                        f"systemd unit '{label}.service' not installed — "
                        f"gateway is probably running manually."
                    ),
                )
            return RestartResult(
                ok=False, method="error",
                detail=f"systemctl restart failed: {err_text}",
            )
        came_back = await _wait_for_port(
            health_check_host, health_check_port, health_check_timeout,
        )
        elapsed = asyncio.get_event_loop().time() - started
        return RestartResult(
            ok=came_back, method="systemctl",
            detail=(
                f"restarted via systemd ({elapsed:.1f}s downtime)"
                if came_back
                else "systemctl restart returned ok but port didn't come back"
                + _stale_exec_hint(label)
            ),
            paused_seconds=elapsed,
        )

    if system == "windows":
        if shutil.which("schtasks") is None:
            return RestartResult(
                ok=False, method="no_service",
                detail="schtasks not found — restart gateway manually",
            )
        # The gateway runs as a Task Scheduler task (see service_cmd.py), NOT a
        # Windows Service — so the old `sc.exe stop/start` path failed with
        # "service does not exist" even though `flowly service stop`/`start`
        # (which use schtasks) work. Bounce the task: /end then /run.
        await _run(["schtasks", "/end", "/tn", label])
        await asyncio.sleep(0.5)
        rc, out, err = await _run(["schtasks", "/run", "/tn", label])
        if rc != 0:
            return RestartResult(
                ok=False, method="error",
                detail=f"schtasks /run failed: {(err or out).strip()}",
            )
        came_back = await _wait_for_port(
            health_check_host, health_check_port, health_check_timeout,
        )
        elapsed = asyncio.get_event_loop().time() - started
        return RestartResult(
            ok=came_back, method="schtasks",
            detail=f"restarted via Task Scheduler ({elapsed:.1f}s downtime)",
            paused_seconds=elapsed,
        )

    return RestartResult(
        ok=False, method="no_service",
        detail=f"unsupported platform: {system}",
    )


# ── helpers ────────────────────────────────────────────────────────


async def _launchctl_loaded(label: str) -> bool:
    """True if ``launchctl list <label>`` exits 0 (job is loaded)."""
    rc, _, _ = await _run(["launchctl", "list", label])
    return rc == 0


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Poll ``host:port`` until something is listening or timeout elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            await asyncio.to_thread(
                lambda: socket.create_connection((host, port), timeout=0.5).close()
            )
            return True
        except OSError:
            await asyncio.sleep(0.25)
    return False
