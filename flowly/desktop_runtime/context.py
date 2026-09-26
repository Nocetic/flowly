"""Explicit host-screen grant for one authenticated chat run."""

from contextvars import ContextVar
from dataclasses import dataclass

from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.run_checkpoint import ScreenRunCheckpoint


@dataclass(frozen=True)
class ScreenRunBinding:
    provider: HostBrowserProvider
    generation: str
    run_id: str
    checkpoint: ScreenRunCheckpoint | None = None


SCREEN_RUN_BINDING: ContextVar[ScreenRunBinding | None] = ContextVar("flowly_screen_run", default=None)
