from __future__ import annotations

import pytest
from textual import on
from textual.app import App

from flowly.tui.app import FlowlyTUI
from flowly.tui.client import ToolComplete
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.status import StatusBar
from flowly.tui.panes.status_panel import SessionStatusPanel


def test_status_bar_reset_context_usage_clears_session_counters(monkeypatch) -> None:
    status = StatusBar()
    status.tokens_in = 12_345
    status.tokens_out = 678
    status.cmp_count = 3
    status.cost_usd = 0.42
    synced = []

    monkeypatch.setattr(status, "_sync_context_header", lambda **kwargs: synced.append(kwargs))

    status.reset_context_usage()

    assert status.tokens_in == 0
    assert status.tokens_out == 0
    assert status.cmp_count == 0
    assert status.cost_usd == 0.0
    assert synced[-1] == {"tokens_in": 0, "tokens_out": 0}


def test_flowly_tui_reset_context_usage_delegates_to_status_bar() -> None:
    calls = []

    class _FakeStatus:
        def reset_context_usage(self) -> None:
            calls.append("reset")

    class _FakeApp:
        def query_one(self, widget_type):
            assert widget_type is StatusBar
            return _FakeStatus()

    FlowlyTUI._reset_context_usage(_FakeApp())  # type: ignore[arg-type]

    assert calls == ["reset"]


async def test_skill_manage_completion_refreshes_command_palette(monkeypatch) -> None:
    app = FlowlyTUI(client=None)
    calls = []

    async def fake_refresh():
        calls.append("refresh")

    monkeypatch.setattr(app, "_refresh_command_palette", fake_refresh)

    did_schedule = app._refresh_command_palette_after_skill_write(
        ToolComplete(
            tool_call_id="tc1",
            name="skill_manage",
            success=True,
            duration_ms=10,
            preview="Skill 'demo' created.",
            session_key="web:1",
        )
    )

    assert did_schedule is True
    # Let the task created by the helper run.
    import asyncio
    await asyncio.sleep(0)
    assert calls == ["refresh"]


async def test_non_skill_tool_completion_does_not_refresh_command_palette(monkeypatch) -> None:
    app = FlowlyTUI(client=None)
    calls = []

    async def fake_refresh():
        calls.append("refresh")

    monkeypatch.setattr(app, "_refresh_command_palette", fake_refresh)

    did_schedule = app._refresh_command_palette_after_skill_write(
        ToolComplete(
            tool_call_id="tc1",
            name="read_file",
            success=True,
            duration_ms=10,
            preview="ok",
            session_key="web:1",
        )
    )

    assert did_schedule is False
    assert calls == []


@pytest.mark.asyncio
async def test_composer_status_panel_replaces_input_row() -> None:
    class _Host(App):
        def compose(self):
            yield Composer()

        @on(SessionStatusPanel.Dismissed)
        def _on_dismissed(self, event: SessionStatusPanel.Dismissed) -> None:
            event.stop()
            self.query_one(Composer).clear_status()

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        composer = app.query_one(Composer)
        composer.show_status(
            session="local:abc",
            provider="openrouter",
            provider_source="config",
            model="anthropic/claude-sonnet-4.5",
            state="idle",
            tokens_in=1234,
            tokens_out=56,
            cost_usd=0.12,
            queued=2,
        )
        await pilot.pause()

        assert composer.has_class("status-open")
        assert not app.query_one("#composer-input-row").display
        assert app.focused is app.query_one(SessionStatusPanel)

        await pilot.press("escape")
        await pilot.pause()

        assert not composer.has_class("status-open")
