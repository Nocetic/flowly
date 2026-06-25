"""Lightweight y/N confirmation modal — used before destructive ops.

Distinct from the LoginModal / ApprovalModal style because the
information density is intentionally low: one prompt, two buttons,
single-keystroke commit (``y`` / ``n`` / ``Esc``). Returns ``True``
when the user confirms, ``False`` otherwise.

Usage:

    decision = await self.push_screen_wait(
        ConfirmModal(
            title="Clear session?",
            body="This will discard 12 messages on disk.",
            confirm_label="Clear",
        )
    )
    if decision:
        await self._do_clear()
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ConfirmModal(ModalScreen[bool]):
    """Returns ``True`` on confirm, ``False`` on cancel / Esc."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 60%;
        max-width: 80;
        height: auto;
        padding: 1 2;
        border: thick #f2c94c;
        background: #050505;
    }
    ConfirmModal .title {
        text-style: bold;
        color: #f2c94c;
        height: 1;
        margin-bottom: 1;
    }
    ConfirmModal .body {
        color: #e6fbff;
        height: auto;
        margin-bottom: 1;
    }
    ConfirmModal .hint {
        color: #83b8c2;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    ConfirmModal Horizontal {
        height: auto;
        align-horizontal: right;
    }
    ConfirmModal Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        # Single-keystroke commit so the modal feels lightweight.
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
        with Vertical():
            yield Label(self._title_text, classes="title")
            yield Static(self._body_text, classes="body")
            yield Label(
                "y / Enter to confirm · n / Esc to cancel",
                classes="hint",
            )
            with Horizontal():
                yield Button(self._cancel_label, id="cancel-btn", variant="default")
                yield Button(self._confirm_label, id="confirm-btn", variant="warning")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-btn")
    def _on_confirm(self, _event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-btn")
    def _on_cancel(self, _event: Button.Pressed) -> None:
        self.dismiss(False)
