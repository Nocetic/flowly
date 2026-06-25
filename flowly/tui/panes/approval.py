"""Modal approval screen for exec.approval.requested events."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ApprovalModal(ModalScreen[str]):
    """Returns one of: 'allow-once', 'allow-always', 'deny'."""

    DEFAULT_CSS = """
    ApprovalModal {
        align: center middle;
    }
    ApprovalModal > Vertical {
        width: 70%;
        max-width: 100;
        height: auto;
        padding: 1 2;
        border: thick $warning;
        background: $surface;
    }
    ApprovalModal .title {
        color: $warning;
        text-style: bold;
    }
    ApprovalModal .cmd {
        margin: 1 0;
        padding: 1;
        background: $boost;
        color: $text;
    }
    ApprovalModal .reasons {
        color: $text-muted;
        margin-bottom: 1;
    }
    ApprovalModal Horizontal {
        height: auto;
        align-horizontal: right;
    }
    ApprovalModal Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        ("escape", "deny", "Deny"),
        ("a", "allow_once", "Allow once"),
        ("s", "allow_always", "Allow always"),
        ("d", "deny", "Deny"),
    ]

    def __init__(self, command: str, reasons: list[str]) -> None:
        super().__init__()
        self._command = command
        self._reasons = reasons

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("⚠  Approval required", classes="title")
            yield Static(self._command or "(no command)", classes="cmd")
            if self._reasons:
                yield Static("Reasons: " + ", ".join(self._reasons), classes="reasons")
            with Horizontal():
                yield Button("Allow once (a)", id="allow-once", variant="primary")
                yield Button("Allow always (s)", id="allow-always", variant="warning")
                yield Button("Deny (d)", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "deny")

    def action_allow_once(self) -> None:
        self.dismiss("allow-once")

    def action_allow_always(self) -> None:
        self.dismiss("allow-always")

    def action_deny(self) -> None:
        self.dismiss("deny")
