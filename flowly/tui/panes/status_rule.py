"""Thin horizontal separator widget between status bar and composer."""

from __future__ import annotations

from textual.widgets import Static


class StatusRule(Static):
    """One-row horizontal line drawn with box-drawing chars.

    Used between the transcript scroll area and the composer to give a
    crisp visual boundary instead of relying solely on a CSS border
    (which sometimes renders thin or color-inverted on legacy terminals).
    """

    DEFAULT_CSS = """
    StatusRule {
        height: 1;
        padding: 0;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def on_mount(self) -> None:
        self._draw()

    def on_resize(self) -> None:
        self._draw()

    def _draw(self) -> None:
        width = max(1, self.size.width or 80)
        self.update("─" * width)
