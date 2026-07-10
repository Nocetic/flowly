"""Assistant / persona picker — start a session keyed to a chosen assistant."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class AssistantPickerPanel(Vertical):
    """Returns: {'name': str, 'model': str, 'description': str} or None."""

    can_focus = True

    class Dismissed(Message):
        def __init__(self, result: dict[str, Any] | None) -> None:
            super().__init__()
            self.result = result

    DEFAULT_CSS = """
    AssistantPickerPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        padding: 0;
        border: none;
        background: transparent;
    }
    AssistantPickerPanel .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    AssistantPickerPanel .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    AssistantPickerPanel OptionList {
        height: 18;
        border: none;
        background: transparent;
    }
    AssistantPickerPanel .footnote {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("q", "cancel", "Close"),
    ]

    def __init__(self, assistants: list[dict[str, Any]]) -> None:
        super().__init__()
        self._assistants = assistants

    def compose(self) -> ComposeResult:
        yield Label("Assistants", classes="title")
        yield Label(
            "↑/↓ navigate · Enter open new session keyed to this assistant · Esc close",
            classes="hint",
        )
        ol = OptionList(id="assistant-list")
        for idx, assistant in enumerate(self._assistants):
            name = str(assistant.get("name", "?"))
            model = str(assistant.get("model", "") or "default")
            desc = str(assistant.get("description", "") or "").replace("\n", " ")
            if len(desc) > 60:
                desc = desc[:58] + "…"
            tag = "[dim](builtin)[/dim]" if assistant.get("builtin") else ""
            ol.add_option(
                Option(
                    f"[b]{name}[/b]  [dim]{model}[/dim]  {tag}\n   [dim]{desc}[/dim]",
                    id=str(idx),
                )
            )
        yield ol
        yield Label(
            "Note: gateway model is process-wide; this only changes session key.",
            classes="footnote",
        )

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        if options.options:
            options.highlighted = 0
        options.focus()

    def on_focus(self) -> None:
        try:
            self.query_one(OptionList).focus()
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = int(event.option.id or "0")
        if 0 <= idx < len(self._assistants):
            a = self._assistants[idx]
            self.post_message(
                self.Dismissed(
                    {
                        "name": str(a.get("name", "")),
                        "model": str(a.get("model", "")),
                        "description": str(a.get("description", "")),
                    }
                )
            )
        else:
            self.post_message(self.Dismissed(None))

    def action_cancel(self) -> None:
        self.post_message(self.Dismissed(None))


class AssistantPicker(ModalScreen[dict[str, Any] | None]):
    """Compatibility wrapper; the chat TUI mounts :class:`AssistantPickerPanel`."""

    BINDINGS = AssistantPickerPanel.BINDINGS

    DEFAULT_CSS = """
    AssistantPicker { align: center middle; }
    AssistantPicker > AssistantPickerPanel {
        width: 75%;
        max-width: 90;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    def __init__(self, assistants: list[dict[str, Any]]) -> None:
        super().__init__()
        self._assistants = assistants

    def compose(self) -> ComposeResult:
        yield AssistantPickerPanel(self._assistants)

    @on(AssistantPickerPanel.Dismissed)
    def _on_dismissed(self, event: AssistantPickerPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)

    def action_cancel(self) -> None:
        self.query_one(AssistantPickerPanel).action_cancel()
