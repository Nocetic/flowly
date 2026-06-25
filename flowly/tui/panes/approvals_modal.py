"""Pending approvals queue modal — view & resolve exec approval requests."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class ApprovalsModal(ModalScreen[dict[str, Any] | None]):
    """Returns: {'id': str, 'decision': str} or None."""

    DEFAULT_CSS = """
    ApprovalsModal { align: center middle; }
    ApprovalsModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        max-height: 30;
        border: thick #00a6c8;
        background: #050505;
        padding: 1 2;
    }
    ApprovalsModal .title { text-style: bold; color: #00a6c8; height: 1; }
    ApprovalsModal .hint  { color: #83b8c2; text-style: italic; height: 1; margin: 1 0; }
    ApprovalsModal ListView { height: 1fr; background: #050505; border: none; }
    ApprovalsModal ListItem {
        background: #101010;
        padding: 1 2;
        margin-bottom: 1;
    }
    ApprovalsModal .cmd { color: #e6fbff; text-style: bold; }
    ApprovalsModal .session { color: #83b8c2; }
    ApprovalsModal Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    ApprovalsModal Button { margin-left: 1; }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q", "dismiss(None)", "Close"),
    ]

    def __init__(self, approvals: list[dict[str, Any]]) -> None:
        super().__init__()
        self._approvals = approvals

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Pending approvals ({len(self._approvals)})", classes="title")
            yield Label(
                "↑/↓ navigate · A allow once · S allow always · D deny · Esc close",
                classes="hint",
            )
            if not self._approvals:
                yield Static("[dim]No pending approvals.[/dim]")
            else:
                items: list[ListItem] = []
                for a in self._approvals:
                    aid = str(a.get("id", ""))
                    cmd = str(a.get("command", ""))
                    sess = str(a.get("sessionKey", ""))
                    items.append(ListItem(
                        Static(f"[b]{cmd}[/b]\n[dim]session={sess}  id={aid}[/dim]"),
                        id=f"item-{aid}",
                    ))
                # Pass children to the constructor — appending before the
                # ListView is mounted raises MountError (crashed F3 whenever
                # there was a pending approval).
                yield ListView(*items)
            with Horizontal():
                yield Button("Allow once (A)", id="allow-once", variant="primary")
                yield Button("Allow always (S)", id="allow-always", variant="warning")
                yield Button("Deny (D)", id="deny", variant="error")

    def _selected_item(self) -> dict[str, Any] | None:
        try:
            lv = self.query_one(ListView)
        except Exception:
            return None
        if lv.index is None or not self._approvals:
            return None
        return self._approvals[lv.index]

    def _selected_id(self) -> str | None:
        item = self._selected_item()
        return None if item is None else str(item.get("id", ""))

    def _dismiss_with(self, decision: str) -> None:
        item = self._selected_item()
        if item is None:
            self.dismiss(None)
            return
        # Don't let "allow always" resolve a request that can't be remembered
        # (e.g. an email send) — that would be a silent no-op.
        if decision == "allow-always" and not item.get("supportsAlways", True):
            return
        self.dismiss({"id": str(item.get("id", "")), "decision": decision})

    @on(Button.Pressed, "#allow-once")
    def _allow_once(self) -> None: self._dismiss_with("allow-once")

    @on(Button.Pressed, "#allow-always")
    def _allow_always(self) -> None: self._dismiss_with("allow-always")

    @on(Button.Pressed, "#deny")
    def _deny(self) -> None: self._dismiss_with("deny")

    def key_a(self) -> None: self._dismiss_with("allow-once")
    def key_s(self) -> None: self._dismiss_with("allow-always")
    def key_d(self) -> None: self._dismiss_with("deny")
