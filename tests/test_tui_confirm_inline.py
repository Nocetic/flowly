from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen

from flowly.tui.panes.composer import Composer
from flowly.tui.panes.confirm_modal import ConfirmModal, ConfirmPanel


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "expected"), [("enter", True), ("n", False)])
async def test_confirm_panel_returns_keyboard_decision(key: str, expected: bool) -> None:
    dismissed: list[bool] = []

    class _Host(App):
        def compose(self):
            yield ConfirmPanel(title="Clear session?", body="Discard 12 messages.")

        @on(ConfirmPanel.Dismissed)
        def _on_dismissed(self, event: ConfirmPanel.Dismissed) -> None:
            dismissed.append(event.confirmed)

    assert not issubclass(ConfirmPanel, ModalScreen)
    assert issubclass(ConfirmModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one(ConfirmPanel)
        await pilot.press(key)
        await pilot.pause()

    assert dismissed == [expected]


@pytest.mark.asyncio
async def test_confirm_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 32)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(
            ConfirmPanel(title="Clear session?", body="Discard 12 messages."),
            inline=True,
        )
        await pilot.pause()

        panel = app.query_one(ConfirmPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is panel

