"""The profile screen claimed by one agent run (see host_screen_run)."""

from dataclasses import dataclass

from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.run_checkpoint import ScreenRunCheckpoint


@dataclass(frozen=True)
class ScreenRunBinding:
    provider: HostBrowserProvider
    generation: str
    run_id: str
    checkpoint: ScreenRunCheckpoint | None = None
    # The conversation the run belongs to: its browser tabs are its own.
    owner: str = ""

