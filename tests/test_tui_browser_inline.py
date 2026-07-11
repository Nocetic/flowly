from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import Switch

from flowly.tui.panes import browser_modal as browser_mod
from flowly.tui.panes.browser_modal import BrowserModal, BrowserPanel
from flowly.tui.panes.composer import Composer


async def _stub_refresh(panel: BrowserPanel) -> None:
    panel._initial_enabled = False
    panel._extension_connected = None
    panel._sync_primary_action()


@pytest.mark.asyncio
async def test_browser_panel_is_plain_widget_focuses_toggle_and_dismisses(
    monkeypatch,
) -> None:
    monkeypatch.setattr(BrowserPanel, "_refresh_state", _stub_refresh)
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield BrowserPanel()

        @on(BrowserPanel.Dismissed)
        def _on_dismissed(self, event: BrowserPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(BrowserPanel, ModalScreen)
    assert issubclass(BrowserModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#enabled-switch", Switch)

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_browser_panel_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    monkeypatch.setattr(BrowserPanel, "_refresh_state", _stub_refresh)
    monkeypatch.setattr(browser_mod, "_open_browser_detached", lambda _url: True)

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 36)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(BrowserPanel(), inline=True)
        await pilot.pause()

        panel = app.query_one(BrowserPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert panel.region.height <= 24
        assert app.focused is app.query_one("#enabled-switch", Switch)

