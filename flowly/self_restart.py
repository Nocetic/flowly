"""A gateway that restarts itself, whoever runs it.

A settings change that attaches at boot (a channel, a tool, voice) needs the
gateway to start again. That used to be asked of the machine's service
manager, which only works when the asking process *is* that service:

* a named agent runs under its host gateway, so ``launchctl kickstart`` of
  the service label bounced the host and every agent instead of itself;
* a gateway started by hand, or under Desktop's own launchd job, or on a
  Linux box without systemd, has no service to ask, so nothing restarted
  while the app had already been told "restarting";
* the restart counted as done once the port answered, which the old
  process still did.

Restarting in place works the same under every owner: the gateway stops the
way a SIGTERM stops it (channels, cron, MCP servers and agents wind down),
then replaces its own program image with the same command line. The process
id stays, so launchd, systemd, a host gateway or a terminal keep the process
they started, and no second gateway can race the first for the port.

Clients are told with a close code (:data:`RESTART_CLOSE_CODE`) on their
socket, which is how a host gateway knows to wait for its agent to come back
rather than stop it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from loguru import logger

#: WebSocket close code a restarting gateway closes its clients with
#: (4000-4999 is the range RFC 6455 leaves to applications).
RESTART_CLOSE_CODE = 4075

#: Set on the restarted process when the gateway it replaces had taken the
#: installed service's port, so the last one to exit puts the service back.
RESTORE_SERVICE_ENV = "FLOWLY_RESTORE_SERVICE_PORT"

_stop: Callable[[], None] | None = None
_drop_flags: frozenset[str] = frozenset()
_requested = False


def install(stop: Callable[[], None], *, drop_flags: tuple[str, ...] = ()) -> None:
    """Let feature restarts stop this gateway; ``stop`` begins its shutdown.

    ``drop_flags`` are one-shot switches the restarted process must not see
    again (``--rotate-token`` would unpair every app on every restart).
    """
    global _stop, _drop_flags
    _stop = stop
    _drop_flags = frozenset(drop_flags)


def uninstall() -> None:
    global _stop
    _stop = None


def available() -> bool:
    """Whether this process can restart itself (a running POSIX gateway)."""
    return _stop is not None and os.name == "posix" and command() is not None


def requested() -> bool:
    return _requested


def request() -> bool:
    """Begin a restart in place. False when this process cannot do one."""
    global _requested
    if not available():
        return False
    _requested = True
    assert _stop is not None
    _stop()
    return True


def command() -> list[str] | None:
    """The command line that started this process, ready for ``execv``.

    Replayed from ``sys.orig_argv``, which keeps the interpreter's flags,
    ``-m`` and the script path.
    """
    argv = list(getattr(sys, "orig_argv", None) or [])
    executable = sys.executable
    if not argv or not executable or not os.access(executable, os.X_OK):
        return None
    return [executable, *_kept(argv[1:])]


def _kept(args: list[str]) -> list[str]:
    return [arg for arg in args if arg not in _drop_flags]


def reexec() -> None:
    """Replace this process with a fresh run of the same command.

    Called after the gateway has fully stopped. Never returns on success; on
    failure it exits with ``EX_TEMPFAIL`` so a supervisor that restarts on
    failure (launchd KeepAlive, systemd Restart=, a host gateway) starts it.
    """
    argv = command()
    if argv is None:
        logger.error("Gateway restart: this process cannot find its own command line")
        raise SystemExit(75)
    logger.info("Gateway restarting in place")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 — a closed stream must not stop the restart
            pass
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        logger.error("Gateway restart failed: {}", exc)
        raise SystemExit(75) from exc
