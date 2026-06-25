"""Assistant / persona picker — start a session keyed to a chosen assistant."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class AssistantPicker(ModalScreen[dict[str, Any] | None]):
    """Returns: {'name': str, 'model': str, 'description': str} or None."""

    DEFAULT_CSS = """
    AssistantPicker {
        align: center middle;
    }
    AssistantPicker > Vertical {
        width: 75%;
        max-width: 90;
        height: 70%;
        max-height: 25;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    AssistantPicker .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    AssistantPicker .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    AssistantPicker OptionList {
        height: 1fr;
        border: none;
    }
    AssistantPicker .footnote {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q", "dismiss(None)", "Close"),
    ]

    def __init__(self, assistants: list[dict[str, Any]]) -> None:
        super().__init__()
        self._assistants = assistants

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Assistants", classes="title")
            yield Label(
                "↑/↓ navigate · Enter open new session keyed to this assistant · Esc close",
                classes="hint",
            )
            ol = OptionList(id="assistant-list")
            for idx, a in enumerate(self._assistants):
                name = str(a.get("name", "?"))
                model = str(a.get("model", "") or "default")
                desc = str(a.get("description", "") or "").replace("\n", " ")
                if len(desc) > 60:
                    desc = desc[:58] + "…"
                tag = "[dim](builtin)[/dim]" if a.get("builtin") else ""
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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = int(event.option.id or "0")
        if 0 <= idx < len(self._assistants):
            a = self._assistants[idx]
            self.dismiss({
                "name": str(a.get("name", "")),
                "model": str(a.get("model", "")),
                "description": str(a.get("description", "")),
            })
        else:
            self.dismiss(None)
