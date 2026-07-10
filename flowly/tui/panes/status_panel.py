"""Composer-inline /status panel."""

from __future__ import annotations

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static


class SessionStatusPanel(Vertical):
    """Small composer prompt panel for current session/provider/model state."""

    can_focus = True

    class Dismissed(Message):
        """Esc/q pressed; the app should close the panel."""

    def compose(self) -> ComposeResult:
        yield Static("Status", id="status-panel-title", markup=False)
        yield Static("", id="status-panel-session", markup=True)
        yield Static("", id="status-panel-provider", markup=True)
        yield Static("", id="status-panel-model", markup=True)
        yield Static("", id="status-panel-state", markup=True)
        yield Static("", id="status-panel-usage", markup=True)
        yield Static("Esc/q close", id="status-panel-hint", markup=False)

    def set_data(
        self,
        *,
        session: str,
        provider: str,
        provider_source: str,
        model: str,
        state: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        queued: int,
    ) -> None:
        source = f"  [dim]{escape(provider_source)}[/]" if provider_source else ""
        self.query_one("#status-panel-session", Static).update(
            f"[dim]Session[/]   [b]{escape(session or '?')}[/]"
        )
        self.query_one("#status-panel-provider", Static).update(
            f"[dim]Provider[/]  [b]{escape(provider or '?')}[/]{source}"
        )
        self.query_one("#status-panel-model", Static).update(
            f"[dim]Model[/]     [b]{escape(model or '?')}[/]"
        )
        self.query_one("#status-panel-state", Static).update(
            f"[dim]State[/]     [b]{escape(state or 'idle')}[/]"
            + (f"  [dim]{queued} queued[/]" if queued else "")
        )
        self.query_one("#status-panel-usage", Static).update(
            f"[dim]Usage[/]     {tokens_in:,} in · {tokens_out:,} out"
            + (f" · ${cost_usd:.4f}" if cost_usd else "")
        )
        self.focus()

    def clear(self) -> None:
        for wid in (
            "status-panel-session",
            "status-panel-provider",
            "status-panel-model",
            "status-panel-state",
            "status-panel-usage",
        ):
            try:
                self.query_one(f"#{wid}", Static).update("")
            except Exception:
                pass

    def on_key(self, event: events.Key) -> None:
        if event.key in ("escape", "q"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Dismissed())
