"""Lightweight y/N confirmation prompt — used before destructive ops.

Distinct from the LoginModal / ApprovalModal style because the
information density is intentionally low: one prompt, two choices,
single-keystroke commit (``y`` / ``n`` / ``Esc``). Returns ``True``
when the user confirms, ``False`` otherwise.

Usage:

    decision = await self._show_composer_picker(
        ConfirmPanel(
            title="Clear session?",
            body="This will discard 12 messages on disk.",
            confirm_label="Clear",
        ),
        inline=True,
    )
    if decision:
        await self._do_clear()
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, Static


class ConfirmPanel(Vertical):
    """Returns ``True`` on confirm, ``False`` on cancel / Esc."""

    can_focus = True

    class Dismissed(Message):
        def __init__(self, confirmed: bool) -> None:
            super().__init__()
            self.confirmed = confirmed

    DEFAULT_CSS = """
    ConfirmPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        padding: 0;
        border: none;
        background: transparent;
    }
    ConfirmPanel .title {
        text-style: bold;
        color: #f2c94c;
        height: 1;
        margin-bottom: 1;
    }
    ConfirmPanel .body {
        color: #e6fbff;
        height: auto;
        margin-bottom: 1;
    }
    ConfirmPanel .hint {
        color: #83b8c2;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    ConfirmPanel .choice {
        height: 1;
        color: #e6fbff;
    }
    """

    BINDINGS = [
        # Single-keystroke commit so the prompt feels lightweight.
        # ``y`` / ``Enter`` confirm, ``n`` / ``Esc`` cancel. Mirrors
        # the standard yes/no prompt convention so it's instantly
        # familiar without reading the button labels.
        ("y",      "confirm", "Confirm"),
        ("enter",  "confirm", "Confirm"),
        ("n",      "cancel",  "Cancel"),
        ("escape", "cancel",  "Cancel"),
        ("q",      "cancel",  "Cancel"),
    ]

    def __init__(
        self,
        *,
        title: str,
        body: str,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self._title_text = title
        self._body_text = body
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        yield Label(self._title_text, classes="title")
        yield Static(self._body_text, classes="body")
        yield Static(f"› Y  {self._confirm_label}", classes="choice")
        yield Static(f"  N  {self._cancel_label}", classes="choice")
        yield Label("Y / Enter confirm · N / Esc cancel", classes="hint")

    def on_mount(self) -> None:
        self.focus()

    def action_confirm(self) -> None:
        self.post_message(self.Dismissed(True))

    def action_cancel(self) -> None:
        self.post_message(self.Dismissed(False))


class ConfirmModal(ModalScreen[bool]):
    """Compatibility wrapper; the chat TUI mounts :class:`ConfirmPanel`."""

    BINDINGS = ConfirmPanel.BINDINGS

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal > ConfirmPanel {
        width: 60%;
        max-width: 80;
        padding: 1 2;
        border: thick #f2c94c;
        background: #050505;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        body: str,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self._args = {
            "title": title,
            "body": body,
            "confirm_label": confirm_label,
            "cancel_label": cancel_label,
        }

    def compose(self) -> ComposeResult:
        yield ConfirmPanel(**self._args)

    @on(ConfirmPanel.Dismissed)
    def _on_dismissed(self, event: ConfirmPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.confirmed)

    def action_confirm(self) -> None:
        self.query_one(ConfirmPanel).action_confirm()

    def action_cancel(self) -> None:
        self.query_one(ConfirmPanel).action_cancel()
