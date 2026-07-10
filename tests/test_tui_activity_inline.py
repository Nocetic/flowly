from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import DataTable

from flowly.tui.panes.activity_modal import ActivityModal, ActivityPanel
from flowly.tui.panes.composer import Composer

ENTRIES = [
    {
        "ts": "2026-07-10T10:20:30Z",
        "type": "llm_call",
        "session": "session-1",
        "model": "gpt-5",
        "finish_reason": "stop",
        "duration_ms": 125,
        "prompt_tokens": 10,
        "completion_tokens": 20,
    }
]
STATS = {
    "files": 1,
    "total_bytes": 1024,
    "oldest_date": "2026-07-10",
    "newest_date": "2026-07-10",
    "retention_days": 30,
}


@pytest.mark.asyncio
async def test_activity_panel_is_plain_widget_focuses_table_and_dismisses() -> None:
    dismissed = 0

    class _Host(App):
        def compose(self):
            yield ActivityPanel(ENTRIES, STATS)

        @on(ActivityPanel.Dismissed)
        def _on_dismissed(self, event: ActivityPanel.Dismissed) -> None:
            nonlocal dismissed
            dismissed += 1

    assert not issubclass(ActivityPanel, ModalScreen)
    assert issubclass(ActivityModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one(DataTable)
        assert app.query_one(DataTable).row_count == 1

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == 1


@pytest.mark.asyncio
async def test_activity_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(140, 36)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(ActivityPanel(ENTRIES, STATS), inline=True)
        await pilot.pause()

        panel = app.query_one(ActivityPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one(DataTable)

