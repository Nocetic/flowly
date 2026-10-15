"""Tell clients that the goals changed, so they read them again.

A goals list (Desktop's agent panel, the phones' "Goals and routines") used to
poll ``objectives.list`` every minute: a goal the agent opened in a chat, a
step ticked on another device or a check-in it moved showed up to a minute
late. Now every committed change to the goals (``objectives.json``) or to what
the clients show of the agenda (a goal's next check-in, the settings) raises
one ``objectives.changed`` event.

The event carries no goal content: a client that hears it reads the goals
again through its own authorized RPC, the same way Board completion works.
Changes that land together (a tool call that updates a goal and moves its
check-in) are coalesced into one event (:data:`COALESCE_SECONDS`).

The stores call :func:`changed` from wherever they run, including worker
threads; the runtime registers the broadcaster once (``flowly/cli/gateway_cmd.py``)
and it is always awaited on the runtime's own loop. Without a broadcaster
(tests, the CLI) a change is simply not announced.

The announcement runs in an empty context, never in the writer's: a change
made inside one account's request would otherwise be scoped to that account
(``flowly/live_voice/events.py`` ``EventAccess.capture``), and the agent's
goals are the agent's, shown to every screen that reads them.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import Any, Awaitable, Callable

from loguru import logger

EVENT = "objectives.changed"
#: Changes this close together are announced once.
COALESCE_SECONDS = 0.15

Broadcaster = Callable[[dict[str, Any]], Awaitable[None]]

_lock = threading.Lock()
_broadcaster: Broadcaster | None = None
_loop: asyncio.AbstractEventLoop | None = None
_pending = False


def set_broadcaster(broadcaster: Broadcaster | None, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Register how this runtime announces a change (None clears it)."""
    global _broadcaster, _loop, _pending
    with _lock:
        _broadcaster = broadcaster
        _loop = (loop or asyncio.get_running_loop()) if broadcaster is not None else None
        _pending = False


def announcing() -> bool:
    """Whether this runtime announces changes (``system.capabilities``)."""
    with _lock:
        return _broadcaster is not None


def changed() -> None:
    """A change was committed. Never raises; never blocks the writer."""
    global _pending
    with _lock:
        loop = _loop
        if _broadcaster is None or loop is None or loop.is_closed() or _pending:
            return
        _pending = True
    try:
        loop.call_soon_threadsafe(_schedule, context=contextvars.Context())
    except RuntimeError:  # the loop is shutting down
        with _lock:
            _pending = False


def _schedule() -> None:
    asyncio.ensure_future(_announce())


async def _announce() -> None:
    global _pending
    await asyncio.sleep(COALESCE_SECONDS)
    with _lock:
        _pending = False
        broadcaster = _broadcaster
    if broadcaster is None:
        return
    try:
        await broadcaster({"at": int(time.time() * 1000)})
    except Exception as exc:  # noqa: BLE001 — an announcement never breaks a write
        logger.debug(f"[objectives] change not announced: {exc}")
