from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import Button

from flowly.tui.panes.composer import Composer
from flowly.tui.panes.policy_modal import PolicyModal, PolicyPanel

POLICY = {
    "security": "allowlist",
    "ask": "on-miss",
    "allowlist": [{"pattern": "/usr/bin/git", "command": "git *"}],
}


@pytest.mark.asyncio
async def test_policy_panel_applies_live_stays_open_and_dismisses() -> None:
    applied: list[dict] = []
    dismissed = 0

    async def apply(action):
        applied.append(action)
        return {"security": "deny", "ask": "on-miss", "allowlist": []}

    class _Host(App):
        def compose(self):
            yield PolicyPanel(POLICY, apply)

        @on(PolicyPanel.Dismissed)
        def _on_dismissed(self, event: PolicyPanel.Dismissed) -> None:
            nonlocal dismissed
            dismissed += 1

    assert not issubclass(PolicyPanel, ModalScreen)
    assert issubclass(PolicyModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#sec-allowlist", Button)

        await pilot.click("#sec-deny")
        await pilot.pause()
        assert applied == [{"action": "set", "security": "deny"}]
        assert app.query_one("#sec-deny", Button).variant == "success"
        assert app.query_one(PolicyPanel).is_mounted

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == 1


@pytest.mark.asyncio
async def test_policy_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 36)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(PolicyPanel(POLICY), inline=True)
        await pilot.pause()

        panel = app.query_one(PolicyPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert panel.region.height <= 24
        assert app.focused is app.query_one("#sec-allowlist", Button)

