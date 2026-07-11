from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import OptionList

from flowly.tui.panes.composer import Composer
from flowly.tui.panes.theme_picker import ThemePicker, ThemePickerPanel
from flowly.tui.theme import list_themes


@pytest.mark.asyncio
async def test_theme_panel_previews_live_and_escape_reverts_via_result() -> None:
    themes = list(list_themes())
    previews: list[str] = []
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield ThemePickerPanel(themes[0].name)

        def preview_theme(self, name: str) -> None:
            previews.append(name)

        @on(ThemePickerPanel.Dismissed)
        def _on_dismissed(self, event: ThemePickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(ThemePickerPanel, ModalScreen)
    assert issubclass(ThemePicker, ModalScreen)

    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#theme-list", OptionList)

        await pilot.press("down")
        await pilot.pause()
        assert previews[-1] == themes[1].name

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_theme_panel_uses_composer_inline_prompt_surface() -> None:
    current = list(list_themes())[0].name

    class _Host(App):
        def compose(self):
            yield Composer()

        def preview_theme(self, _name: str) -> None:
            pass

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(ThemePickerPanel(current), inline=True)
        await pilot.pause()

        panel = app.query_one(ThemePickerPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one("#theme-list", OptionList)

