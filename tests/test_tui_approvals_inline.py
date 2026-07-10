from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import ListView

from flowly.tui.panes.approvals_modal import ApprovalsModal, ApprovalsPanel
from flowly.tui.panes.composer import Composer

APPROVALS = [
    {
        "id": "approval-1",
        "command": "git push",
        "sessionKey": "tui:1",
        "supportsAlways": True,
    },
    {
        "id": "approval-2",
        "command": "send email",
        "sessionKey": "tui:1",
        "supportsAlways": False,
    },
]


@pytest.mark.asyncio
async def test_approvals_panel_blocks_unsupported_always_and_returns_decision() -> None:
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield ApprovalsPanel(APPROVALS)

        @on(ApprovalsPanel.Dismissed)
        def _on_dismissed(self, event: ApprovalsPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(ApprovalsPanel, ModalScreen)
    assert issubclass(ApprovalsModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one(ListView)

        await pilot.press("down")
        await pilot.press("s")
        await pilot.pause()
        assert dismissed == []

        await pilot.press("d")
        await pilot.pause()

    assert dismissed == [{"id": "approval-2", "decision": "deny"}]


@pytest.mark.asyncio
async def test_approvals_panel_uses_composer_inline_prompt_surface() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(ApprovalsPanel(APPROVALS), inline=True)
        await pilot.pause()

        panel = app.query_one(ApprovalsPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one(ListView)

