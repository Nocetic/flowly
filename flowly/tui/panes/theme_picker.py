"""ThemePicker — switch Flowly TUI color palettes."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from flowly.tui.theme import FlowlyPalette, list_themes


class ThemePicker(ModalScreen[str | None]):
    """Dismisses with the selected theme name, or None on cancel."""

    DEFAULT_CSS = """
    ThemePicker { align: center middle; }
    ThemePicker > Vertical {
        width: 72%;
        max-width: 86;
        height: auto;
        max-height: 24;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    ThemePicker .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    ThemePicker .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    ThemePicker OptionList {
        height: auto;
        max-height: 14;
        border: none;
        background: $surface;
    }
    ThemePicker .footer {
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

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current
        self._preview = current
        self._themes: list[FlowlyPalette] = list(list_themes())
        self._row_index: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Theme", classes="title")
            yield Label("↑/↓ preview live · Enter apply · Esc revert", classes="hint")
            yield OptionList(
                *[
                    Option(self._row_text(theme), id=theme.name)
                    for theme in self._themes
                ],
                id="theme-list",
            )
            yield Label("", id="theme-footer", classes="footer")

    def on_mount(self) -> None:
        self._row_index = {theme.name: idx for idx, theme in enumerate(self._themes)}
        ol = self.query_one("#theme-list", OptionList)
        for idx, theme in enumerate(self._themes):
            if theme.name == self._current:
                ol.highlighted = idx
                break
        self._set_preview(self._current)

    def _row_text(self, theme: FlowlyPalette) -> str:
        marker = "›" if theme.name == self._preview else " "
        state = ""
        if theme.name == self._current:
            state = "[dim]saved[/dim]"
        if theme.name == self._preview and theme.name != self._current:
            state = "[cyan]preview[/cyan]"
        sample = (
            f"[{theme.accent}]accent[/] "
            f"[{theme.success}]ok[/] "
            f"[{theme.warning}]warn[/] "
            f"[{theme.error}]err[/]"
        )
        desc = theme.description
        if len(desc) > 36:
            desc = desc[:33] + "…"
        return (
            f"{marker} [b]{theme.label:<10}[/b] "
            f"[dim]{theme.name:<11}[/dim]  "
            f"{desc:<36}  {state:<18} {sample}"
        )

    def _set_preview(self, name: str) -> None:
        if name not in self._row_index:
            return
        previous = self._preview
        self._preview = name
        self._refresh_row(previous)
        self._refresh_row(name)
        theme = next((t for t in self._themes if t.name == name), None)
        if theme is not None:
            self.query_one("#theme-footer", Label).update(
                f"previewing {theme.label} ({theme.name}) · Enter apply · Esc revert"
            )
        preview = getattr(self.app, "preview_theme", None)
        if callable(preview):
            preview(name)

    def _refresh_row(self, name: str) -> None:
        idx = self._row_index.get(name)
        if idx is None:
            return
        theme = self._themes[idx]
        try:
            self.query_one("#theme-list", OptionList).replace_option_prompt_at_index(
                idx,
                self._row_text(theme),
            )
        except Exception:
            pass

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        name = str(event.option.id or "")
        if name:
            self._set_preview(name)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        name = str(event.option.id or "")
        self.dismiss(name or None)
