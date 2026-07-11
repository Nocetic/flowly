"""Pending approvals queue modal — view & resolve exec approval requests."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static


class ApprovalsPanel(Vertical):
    """Returns: {'id': str, 'decision': str} or None."""

    can_focus = True

    class Dismissed(Message):
        def __init__(self, result: dict[str, Any] | None) -> None:
            super().__init__()
            self.result = result

    DEFAULT_CSS = """
    ApprovalsPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        border: none;
        background: transparent;
        padding: 0;
    }
    ApprovalsPanel .title { text-style: bold; color: #00a6c8; height: 1; }
    ApprovalsPanel .hint  { color: #83b8c2; text-style: italic; height: 1; margin: 1 0; }
    ApprovalsPanel ListView { height: 18; background: transparent; border: none; }
    ApprovalsPanel ListItem {
        background: transparent;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("q", "cancel", "Close"),
        ("a", "allow_once", "Allow once"),
        ("s", "allow_always", "Allow always"),
        ("d", "deny", "Deny"),
    ]

    def __init__(self, approvals: list[dict[str, Any]]) -> None:
        super().__init__()
        self._approvals = approvals

    def compose(self) -> ComposeResult:
        yield Label(f"Pending approvals ({len(self._approvals)})", classes="title")
        yield Label(
            "↑/↓ navigate · A allow once · S allow always · D deny · Esc close",
            classes="hint",
        )
        if not self._approvals:
            yield Static("[dim]No pending approvals.[/dim]")
        else:
            items: list[ListItem] = []
            for approval in self._approvals:
                aid = str(approval.get("id", ""))
                cmd = str(approval.get("command", ""))
                sess = str(approval.get("sessionKey", ""))
                items.append(
                    ListItem(
                        Static(f"[b]{cmd}[/b]\n[dim]session={sess}  id={aid}[/dim]"),
                        id=f"item-{aid}",
                    )
                )
            yield ListView(*items)

    def on_mount(self) -> None:
        try:
            self.query_one(ListView).focus()
        except Exception:
            self.focus()

    def on_focus(self) -> None:
        try:
            self.query_one(ListView).focus()
        except Exception:
            pass

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
            self.post_message(self.Dismissed(None))
            return
        # Don't let "allow always" resolve a request that can't be remembered
        # (e.g. an email send) — that would be a silent no-op.
        if decision == "allow-always" and not item.get("supportsAlways", True):
            return
        self.post_message(
            self.Dismissed({"id": str(item.get("id", "")), "decision": decision})
        )

    def action_allow_once(self) -> None:
        self._dismiss_with("allow-once")

    def action_allow_always(self) -> None:
        self._dismiss_with("allow-always")

    def action_deny(self) -> None:
        self._dismiss_with("deny")

    def action_cancel(self) -> None:
        self.post_message(self.Dismissed(None))


class ApprovalsModal(ModalScreen[dict[str, Any] | None]):
    """Compatibility wrapper; the chat TUI mounts :class:`ApprovalsPanel`."""

    BINDINGS = ApprovalsPanel.BINDINGS

    DEFAULT_CSS = """
    ApprovalsModal { align: center middle; }
    ApprovalsModal > ApprovalsPanel {
        width: 90%;
        max-width: 120;
        padding: 1 2;
        border: thick #00a6c8;
        background: #050505;
    }
    """

    def __init__(self, approvals: list[dict[str, Any]]) -> None:
        super().__init__()
        self._approvals = approvals

    def compose(self) -> ComposeResult:
        yield ApprovalsPanel(self._approvals)

    @on(ApprovalsPanel.Dismissed)
    def _on_dismissed(self, event: ApprovalsPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)

    def action_cancel(self) -> None:
        self.query_one(ApprovalsPanel).action_cancel()
