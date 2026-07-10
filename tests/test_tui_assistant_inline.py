from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import OptionList

from flowly.tui.panes.assistant_picker import AssistantPicker, AssistantPickerPanel
from flowly.tui.panes.composer import Composer

ASSISTANTS = [
    {"name": "general", "model": "gpt-5", "description": "General assistant"},
    {"name": "reviewer", "model": "gpt-5", "description": "Code reviewer"},
]


@pytest.mark.asyncio
async def test_assistant_panel_focuses_list_and_returns_selection() -> None:
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield AssistantPickerPanel(ASSISTANTS)

        @on(AssistantPickerPanel.Dismissed)
        def _on_dismissed(self, event: AssistantPickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(AssistantPickerPanel, ModalScreen)
    assert issubclass(AssistantPicker, ModalScreen)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#assistant-list", OptionList)
        await pilot.press("down", "enter")
        await pilot.pause()

    assert dismissed == [ASSISTANTS[1]]


@pytest.mark.asyncio
async def test_assistant_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(AssistantPickerPanel(ASSISTANTS), inline=True)
        await pilot.pause()

        panel = app.query_one(AssistantPickerPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one("#assistant-list", OptionList)
