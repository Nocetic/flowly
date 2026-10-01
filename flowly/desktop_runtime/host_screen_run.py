"""The browser of an agent run on a host whose profile screen is running.

A bot uses the browser of the machine it runs on. When this profile's screen
runs on this host, every agent run of the profile (Desktop, relay, channels,
routines) uses that screen's browser; nobody selects it per message.

Entering the scope claims nothing. The screen is claimed for the run at its
first browser action and released when the run ends, so runs that never touch
the browser never contend for it. The decision is made once per run: a run
that started on another browser does not switch to the screen mid-task.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from loguru import logger

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.context import ScreenRunBinding
from flowly.desktop_runtime.run_checkpoint import ScreenRunCheckpoint


class HostScreenRun:
    def __init__(self, gateway, session_key: str, run_id: str):
        self.gateway, self.session_key, self.run_id = gateway, session_key, run_id
        self.binding: ScreenRunBinding | None = None
        self.decided = False
        self.succeeded = False
        self._lock = asyncio.Lock()

    def _running(self):
        try:
            _, provider = self.gateway._get_host_screen_browser()
            state = provider.runtime.status()
        except (ScreenError, OSError):
            return None, None
        if not state or state.get("phase") != "running":
            return None, None
        return provider, state["generation"]

    def uses_screen(self) -> bool:
        """Whether this run's browser is the screen; never claims it."""
        if self.decided:
            return self.binding is not None
        return self._running()[0] is not None

    async def bind(self) -> ScreenRunBinding | None:
        """Claim the screen for this run on its first browser action."""
        async with self._lock:
            if self.decided:
                return self.binding
            provider, generation = await asyncio.to_thread(self._running)
            if provider is None:
                self.decided = True
                return None
            identity = await asyncio.to_thread(self.gateway._host_screen_checkpoint_identity)
            # A refused claim (human control, another run) leaves the run
            # undecided, so a later action can claim the same screen.
            checkpoint = await ScreenRunCheckpoint.start(provider, identity, self.session_key,
                                                         self.run_id, generation)
            self.binding = ScreenRunBinding(provider, generation, self.run_id, checkpoint)
            self.decided = True
            return self.binding

    async def finish(self) -> None:
        if self.binding is None or self.binding.checkpoint is None:
            return
        try:
            await self.binding.checkpoint.finish(cancelled=not self.succeeded)
        except (Exception, asyncio.CancelledError):
            # A failed/unknown browser outcome deliberately retains its durable
            # fence; do not log browser payloads during cleanup.
            logger.warning("[Screen] run cleanup requires runtime recovery")


_UNAVAILABLE = {
    "HUMAN_HAS_CONTROL": "The person has control of this screen. Ask them to give control back, then try again.",
    "SCREEN_BUSY": "Another task is using this screen's browser. Try again when it finishes.",
}


def unavailable(exc: ScreenError) -> dict:
    """A browser tool result for a screen the run could not claim."""
    return {"error": _UNAVAILABLE.get(exc.code, "The screen browser is unavailable."), "error_code": exc.code}


HOST_SCREEN_RUN: ContextVar[HostScreenRun | None] = ContextVar("flowly_host_screen_run", default=None)


def current_screen_run() -> HostScreenRun | None:
    return HOST_SCREEN_RUN.get()


def bound_screen_run() -> ScreenRunBinding | None:
    """The screen claimed by the current run, or None before its first browser action."""
    run = HOST_SCREEN_RUN.get()
    return run.binding if run is not None else None


@asynccontextmanager
async def host_screen_run_scope(gateway, session_key: str, run_id: str | None):
    """Scope one agent run; each run gets its own, nested runs included."""
    if gateway is None or sys.platform != "linux":
        # Profile screens exist only on Linux hosts; elsewhere the bot's
        # browser is the machine's own, reached as before.
        token = HOST_SCREEN_RUN.set(None)
        try:
            yield None
        finally:
            HOST_SCREEN_RUN.reset(token)
        return
    run = HostScreenRun(gateway, session_key, run_id or f"screen-{uuid.uuid4().hex}")
    token = HOST_SCREEN_RUN.set(run)
    try:
        yield run
    finally:
        try:
            await run.finish()
        finally:
            HOST_SCREEN_RUN.reset(token)
