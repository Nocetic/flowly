from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.containers import VerticalScroll
from textual.screen import ModalScreen

from flowly.tui.panes.composer import Composer
from flowly.tui.panes.help_modal import HelpModal, HelpPanel


@pytest.mark.asyncio
async def test_help_panel_focuses_scroll_and_dismisses() -> None:
    dismissed = 0

    class _Host(App):
        def compose(self):
            yield HelpPanel()

        @on(HelpPanel.Dismissed)
        def _on_dismissed(self, event: HelpPanel.Dismissed) -> None:
            nonlocal dismissed
            dismissed += 1

    assert not issubclass(HelpPanel, ModalScreen)
    assert issubclass(HelpModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#help-scroll", VerticalScroll)

        await pilot.press("?")
        await pilot.pause()

    assert dismissed == 1


@pytest.mark.asyncio
async def test_help_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 36)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(HelpPanel(), inline=True)
        await pilot.pause()

        panel = app.query_one(HelpPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert panel.region.height <= 24
        assert app.focused is app.query_one("#help-scroll", VerticalScroll)
