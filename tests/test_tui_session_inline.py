from __future__ import annotations

import time

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import OptionList

from flowly.tui.panes.composer import Composer
from flowly.tui.panes.session_picker import SessionPicker, SessionPickerPanel

SESSIONS = [
    {"key": "", "displayName": "invalid"},
    {"key": "tui:one", "displayName": "One", "updatedAt": time.time() * 1000},
    {"key": "tui:two", "displayName": "Two", "updatedAt": time.time() * 1000},
]


@pytest.mark.asyncio
async def test_session_panel_filters_empty_keys_and_requires_two_delete_presses() -> None:
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield SessionPickerPanel(SESSIONS, "tui:one")

        @on(SessionPickerPanel.Dismissed)
        def _on_dismissed(self, event: SessionPickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(SessionPickerPanel, ModalScreen)
    assert issubclass(SessionPicker, ModalScreen)

    app = _Host()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        options = app.query_one("#session-list", OptionList)
        assert len(options.options) == 2
        assert options.highlighted == 0
        assert app.focused is options

        await pilot.press("d")
        await pilot.pause()
        assert dismissed == []

        await pilot.press("d")
        await pilot.pause()

    assert dismissed == [{"action": "delete", "sessionKey": "tui:one"}]


@pytest.mark.asyncio
async def test_session_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(SessionPickerPanel(SESSIONS, "tui:one"), inline=True)
        await pilot.pause()

        panel = app.query_one(SessionPickerPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one("#session-list", OptionList)

