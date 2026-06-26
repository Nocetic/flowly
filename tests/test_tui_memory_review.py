"""Tests for the inline memory-review panel (flowly/tui/panes/memory_review.py),
mounted in the Composer and driven over the Textual test harness."""

from __future__ import annotations

import pytest

from flowly.tui.panes.composer import Composer
from flowly.tui.panes.memory_review import MemoryReviewPanel

_ITEM = {"id": "m_1", "kind": "preference", "text": "Uses React", "confidence": 0.62}


def _host(decisions: list[str]):
    from textual.app import App

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).show_memory_review(_ITEM, 0, 2)

        def on_memory_review_panel_decision(
            self, event: MemoryReviewPanel.Decision
        ) -> None:
            decisions.append(event.action)

    return _Host()


@pytest.mark.asyncio
async def test_panel_shows_and_shortcut_keys_decide() -> None:
    decisions: list[str] = []
    app = _host(decisions)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.has_class("review-open")
        # The panel is focused, so the shortcut keys route to it.
        await pilot.press("a")  # keep
        await pilot.press("r")  # discard
        await pilot.press("s")  # skip
        await pilot.press("escape")  # close
        await pilot.pause()
    assert decisions == ["keep", "discard", "skip", "close"]


@pytest.mark.asyncio
async def test_panel_arrow_navigation_then_enter() -> None:
    decisions: list[str] = []
    app = _host(decisions)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Selection starts on "keep"; down → "discard"; Enter selects it.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert decisions == ["discard"]


def test_set_item_is_idempotent_without_mount() -> None:
    # _move wraps within the fixed action set, independent of any mounted rows.
    panel = MemoryReviewPanel()
    panel._selected_idx = 0
    panel._move(-1)
    assert panel._selected_idx == 2  # wraps to "skip"
    panel._move(1)
    assert panel._selected_idx == 0
