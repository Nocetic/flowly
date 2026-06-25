"""Regression: the F3 pending-approvals modal must mount when there are
pending items. It used to ListView.append() before the ListView was mounted,
which raised MountError and crashed the queue whenever anything was pending.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import ListItem

from flowly.tui.panes.approvals_modal import ApprovalsModal


@pytest.mark.asyncio
async def test_approvals_modal_mounts_with_pending_items():
    approvals = [
        {"id": "a1", "command": "git push", "sessionKey": "tui:1", "supportsAlways": True},
        {"id": "a2", "command": "📧 Send email", "sessionKey": "tui:1", "supportsAlways": False},
    ]

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(ApprovalsModal(approvals))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Both rows rendered, no MountError.
        assert len(app.screen.query(ListItem)) == 2


@pytest.mark.asyncio
async def test_approvals_modal_mounts_when_empty():
    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(ApprovalsModal([]))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen.query(ListItem)) == 0
