"""Inline help hint — small floating panel triggered by `?` keystroke.

Shows when the user types a lone `?` in the empty composer.
Disappears as soon as they backspace or type anything else. Provides a
3-second-readable overview of the most common commands and hotkeys.
"""

from __future__ import annotations

from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

COMMON_COMMANDS = [
    ("/help",       "full /help modal"),
    ("/new",        "start a fresh session"),
    ("/clear",      "wipe current session"),
    ("/compact",    "summarize history"),
    ("/sessions",   "switch saved session"),
    ("/assistants", "pick a persona"),
    ("/abort",      "cancel turn"),
    ("/quit",       "exit"),
]

HOTKEYS = [
    ("Ctrl+S",   "sessions picker"),
    ("Ctrl+M",   "assistants picker"),
    ("Ctrl+A",   "toggle subagents (/subs)"),
    ("Ctrl+E",   "edit draft in $EDITOR"),
    ("F1..F4",   "help · activity · approvals · artifacts"),
]


def build_hint(accent: str = "#00a6c8", muted: str = "#83b8c2",
               label: str = "#e6fbff") -> Group:
    title = Text("? quick help · type /help for full · backspace to dismiss",
                 style=f"bold {accent}")

    def two_col(rows: list[tuple[str, str]]) -> Table:
        t = Table.grid(padding=(0, 2))
        t.add_column(style=label, no_wrap=True)
        t.add_column(style=muted)
        for k, v in rows:
            t.add_row(k, v)
        return t

    return Group(
        title,
        Padding(Text("Common commands", style=f"bold {muted}"), (1, 0, 0, 0)),
        two_col(COMMON_COMMANDS),
        Padding(Text("Hotkeys", style=f"bold {muted}"), (1, 0, 0, 0)),
        two_col(HOTKEYS),
    )


class HelpHint(Static):
    DEFAULT_CSS = """
    HelpHint {
        dock: bottom;
        offset-y: -6;
        height: auto;
        max-height: 18;
        width: 60;
        padding: 1 2;
        background: #050505;
        border: round #00a6c8;
        display: none;
        layer: overlay;
    }
    HelpHint.visible { display: block; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("", markup=False, *args, **kwargs)

    def on_mount(self) -> None:
        self.update(build_hint())

    def show(self) -> None:
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")
