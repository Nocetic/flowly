"""Start the local gateway on demand, so ``flowly`` is the only command.

The gateway is a separate process from the chat UI, and until now that
implementation detail was the user's problem: a configured install that
happened to have no running gateway printed

    Gateway not reachable on 127.0.0.1:18790.
    Start the gateway, then run flowly again:
      flowly service install --start
      flowly gateway

— a product telling someone to run the command it could have run itself.
This module runs it.

What it will and won't do
-------------------------
It only ever touches the *local* gateway for the *configured* port, and
only from a real terminal:

* a non-loopback host means the gateway lives on another machine — there is
  nothing here to start;
* a port that isn't the configured one came from a one-off ``--port`` flag,
  so we may start an already-installed unit but never bake that port into a
  new one;
* a non-TTY caller is Flowly Desktop (it spawns this binary as a subprocess
  and owns the gateway lifecycle itself) or a script — both must be left
  alone;
* ``FLOWLY_NO_GATEWAY_AUTOSTART=1`` opts out entirely.

Failure is always a report, never an exception: the caller falls back to
telling the user how to do it by hand.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT = 25.0
_POLL_INTERVAL = 0.4
_OPT_OUT_ENV = "FLOWLY_NO_GATEWAY_AUTOSTART"


@dataclass(frozen=True)
class AutostartResult:
    """What happened when we tried to make the gateway reachable.

    ``running`` is the only thing a caller must branch on. ``attempted``
    separates "we tried and it didn't work" from "this wasn't ours to
    start", which changes what the user should be told. ``installed_unit``
    says whether a persistent background service was created, because that
    is a change to their machine and deserves a line of output.
    """

    running: bool
    attempted: bool = False
    installed_unit: bool = False
    detail: str = ""


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_loopback(host: str) -> bool:
    from flowly.gateway.auth import is_loopback_host

    return is_loopback_host(host)


def _configured_port() -> int:
    """The port the installed service would use — 18790 unless configured."""
    try:
        from flowly.config.loader import load_config

        return int(load_config().gateway.port)
    except Exception:
        return 18790


def _unit_installed() -> bool:
    """True when a launchd/systemd/Windows unit for the gateway exists."""
    try:
        from flowly.cli.service_cmd import DEFAULT_SERVICE_LABEL, _service_paths

        return any(p is not None and p.exists() for p in _service_paths(DEFAULT_SERVICE_LABEL))
    except Exception:
        return False


def _flowly_argv() -> list[str]:
    from flowly.cli.service_cmd import _resolve_flowly_exec_argv

    return _resolve_flowly_exec_argv()


def _autostart_allowed(host: str, port: int) -> tuple[bool, str]:
    if os.environ.get(_OPT_OUT_ENV, "").strip() == "1":
        return False, f"{_OPT_OUT_ENV}=1"
    if not _is_loopback(host):
        return False, f"{host} is not this machine"
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        # Desktop / scripts: never spawn a service behind their back.
        return False, "not an interactive terminal"
    return True, ""


def ensure_gateway_running(
    host: str,
    port: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> AutostartResult:
    """Make the local gateway reachable, starting it if that's ours to do."""
    if _port_open(host, port):
        return AutostartResult(running=True)

    allowed, why = _autostart_allowed(host, port)
    if not allowed:
        return AutostartResult(running=False, detail=why)

    install_unit = not _unit_installed()
    if install_unit and port != _configured_port():
        # A one-off --port. Starting the installed unit would bind the
        # configured port instead, and installing one would persist a port
        # the user never asked to keep.
        return AutostartResult(
            running=False, detail=f"port {port} is not the configured gateway port"
        )

    argv = list(_flowly_argv())
    argv += ["service", "install", "--start"] if install_unit else ["service", "start"]

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a failed start must not kill the CLI
        return AutostartResult(running=False, attempted=True, detail=str(exc))

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return AutostartResult(
            running=False,
            attempted=True,
            installed_unit=False,
            detail=_last_line(detail) or f"exit {completed.returncode}",
        )

    # `service install --start` already waits for the gateway's health
    # endpoint, but `service start` may return before the socket is up, and
    # either way the port is what the TUI is about to dial.
    if not _wait_for_port(host, port, timeout=timeout):
        return AutostartResult(
            running=False,
            attempted=True,
            installed_unit=install_unit,
            detail=f"the service started but nothing is listening on {host}:{port}",
        )

    return AutostartResult(running=True, attempted=True, installed_unit=install_unit)


def _wait_for_port(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _port_open(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL)


def _last_line(text: str) -> str:
    """The last non-empty line — service managers bury the reason at the end."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
