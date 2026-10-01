"""The browser of an agent run on a host where its profile screen is installed.

A bot uses the browser of the machine it runs on. When this host can run this
profile's screen, every agent run of the profile (Desktop, relay, channels,
routines) uses that screen's browser; nobody selects it per message. A stopped
screen (never started, or stopped while unused) is started for the run.

Entering the scope claims nothing. The screen is started if needed and claimed
for the run at its first browser action, and released when the run ends, so
runs that never touch the browser never contend for it. The decision is made
once per run: a run never switches browsers mid-task.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from loguru import logger

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.context import ScreenRunBinding
from flowly.desktop_runtime.run_checkpoint import ScreenRunCheckpoint

# A cold start (display, desktop and browser) normally takes seconds; the
# lifecycle worker's own deadline is 120 s.
START_TIMEOUT_SECONDS = 150
_START_POLL_SECONDS = 0.5


def _start_step(gateway, pinned: dict, operation: str, submitted: bool) -> tuple[dict | None, dict | None]:
    """One observation: (running state, None) or (None, the start operation or None)."""
    from flowly.desktop_runtime.lifecycle import ScreenLifecycle
    from flowly.gateway.display import _resolve

    session = _resolve(gateway, pinned, pinned=True)
    state = session.runtime.status()
    if state and state.get("phase") == "running":
        return state, None
    lifecycle = ScreenLifecycle(session.runtime, session.identity, lambda: _resolve(gateway, pinned, pinned=True))
    if submitted:
        return None, lifecycle.status(operation)
    try:
        return None, lifecycle.submit("start", state["generation"] if state else None, operation)
    except ScreenError as exc:
        # Someone else (the Screen panel, another run) is starting or stopping
        # it, or started it since this observation: observe again.
        if exc.code in {"LIFECYCLE_BUSY", "STALE_GENERATION"}:
            return None, None
        raise


async def start_profile_screen(gateway) -> None:
    """Start this profile's installed screen for an agent and wait until it runs.

    Uses the same durable lifecycle operation as the Screen panel, so the
    panel sees the start and the two never race to start a second runtime.
    """
    identity = await asyncio.to_thread(gateway._host_screen_checkpoint_identity)
    pinned = {"profileId": identity["profileId"], "expectedHostId": identity["hostId"],
              "expectedBotId": identity["botId"]}
    operation = f"agent-start-{uuid.uuid4().hex}"
    submitted = False
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    logger.info("[Screen] starting the screen of profile {} for an agent task", identity["profileId"])
    while time.monotonic() < deadline:
        state, record = await asyncio.to_thread(_start_step, gateway, pinned, operation, submitted)
        if state is not None:
            return
        if record is not None and record["operationId"] == operation:
            submitted = True
            if record["state"] == "failed" and record["error"] != "STALE_GENERATION":
                raise ScreenError(record["error"])
            if record["state"] in {"failed", "succeeded"}:
                # Started or stopped by someone else since this observation
                # (compare-and-start refused it), or stopped again before this
                # run could claim it: observe again.
                operation, submitted = f"agent-start-{uuid.uuid4().hex}", False
        await asyncio.sleep(_START_POLL_SECONDS)
    raise ScreenError("RUNTIME_START_TIMEOUT")


class HostScreenRun:
    def __init__(self, gateway, session_key: str, run_id: str):
        self.gateway, self.session_key, self.run_id = gateway, session_key, run_id
        self.binding: ScreenRunBinding | None = None
        self.decided = False
        self.succeeded = False
        self._lock = asyncio.Lock()

    def _observe(self):
        """(provider, state) of this profile's screen, or (None, None) when this
        host cannot run it (not installed, unsupported, profile unavailable)."""
        from flowly.desktop_runtime.launcher import ScreenRuntime

        try:
            _, provider = self.gateway._get_host_screen_browser()
            state = provider.runtime.status()
        except (ScreenError, OSError):
            return None, None
        if state and state.get("phase") == "running":
            return provider, state
        return (provider, state) if ScreenRuntime.capabilities()["supported"] else (None, None)

    def uses_screen(self) -> bool:
        """Whether this run's browser is the screen; never starts or claims it."""
        if self.decided:
            return self.binding is not None
        return self._observe()[0] is not None

    async def bind(self) -> ScreenRunBinding | None:
        """Start the screen if needed and claim it for this run on its first browser action."""
        async with self._lock:
            if self.decided:
                return self.binding
            provider, state = await asyncio.to_thread(self._observe)
            if provider is None:
                self.decided = True
                return None
            if not state or state.get("phase") != "running":
                # A failed start leaves the run undecided; a later action retries.
                await start_profile_screen(self.gateway)
                state = await asyncio.to_thread(provider.runtime.status)
                if not state or state.get("phase") != "running":
                    raise ScreenError("SCREEN_NOT_RUNNING")
            generation = state["generation"]
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
    "INSUFFICIENT_RESOURCES": "The server does not have enough free memory to start the screen right now.",
    "INSTALL_BUSY": "The screen components are being installed. Try again when the installation finishes.",
    "RUNTIME_START_TIMEOUT": "The screen did not start in time. Try again.",
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
