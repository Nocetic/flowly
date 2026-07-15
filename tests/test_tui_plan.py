"""TUI plan mode: client dispatch + RPC params, the composer plan strip and
approval tray, and the markup-crash regression (tool/plan content with
bracket sequences must NEVER be parsed as style markup)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from rich.text import Text
from textual.app import App, ComposeResult

from flowly.tui.client import (
    GatewayClient,
    PlanApprovalRequested,
    PlanUpdated,
)
from flowly.tui.panes.composer import (
    PlanApprovalPrompt,
    PlanPanel,
    _plan_steps_text,
)

HOSTILE = "[/dim] kötü [b]içerik [unknown]tag [i]x"


def _plan(status: str = "awaiting_approval", revision: int = 3) -> dict[str, Any]:
    return {
        "id": "plan_t1",
        "sessionKey": "tui:default",
        "status": status,
        "revision": revision,
        "title": f"Refactor {HOSTILE}",
        "goal": "goal text",
        "progress": {"total": 3, "completed": 1, "skipped": 0},
        "approval": {"id": "pa_1", "revision": revision, "expiresAt": 9e12},
        "steps": [
            {"id": 1, "content": f"Done {HOSTILE}", "status": "completed"},
            {"id": 2, "content": "Current step", "status": "in_progress"},
            {"id": 3, "content": "Later step", "status": "pending"},
        ],
    }


# ── client: event dispatch ──────────────────────────────────────────────


def _bare_client() -> GatewayClient:
    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()
    client._pending = {}
    return client


@pytest.mark.asyncio
async def test_dispatch_plan_updated():
    client = _bare_client()
    await client._dispatch({"type": "event", "event": "plan.updated", "data": _plan()})
    ev = client._inbox.get_nowait()
    assert isinstance(ev, PlanUpdated)
    assert ev.plan["id"] == "plan_t1"


@pytest.mark.asyncio
async def test_dispatch_plan_approval_requested():
    client = _bare_client()
    await client._dispatch(
        {"type": "event", "event": "plan.approval.requested", "data": _plan()}
    )
    ev = client._inbox.get_nowait()
    assert isinstance(ev, PlanApprovalRequested)
    assert ev.plan["status"] == "awaiting_approval"


# ── client: RPC params ──────────────────────────────────────────────────


def _client_capturing(reply: dict[str, Any]):
    client = GatewayClient.__new__(GatewayClient)
    sent: dict[str, Any] = {}

    async def fake_rpc(method: str, params: dict[str, Any]) -> str:
        sent["method"] = method
        sent["params"] = params
        return "rid-1"

    async def fake_await_reply(rid: str, timeout: float = 5.0) -> dict[str, Any]:
        return reply

    client._rpc = fake_rpc  # type: ignore[method-assign]
    client._await_reply = fake_await_reply  # type: ignore[method-assign]
    return client, sent


@pytest.mark.asyncio
async def test_plan_get_params():
    client, sent = _client_capturing({"plan": _plan()})
    plan = await client.plan_get("tui:default")
    assert sent["method"] == "plan.get"
    assert sent["params"] == {"sessionKey": "tui:default"}
    assert plan["id"] == "plan_t1"


@pytest.mark.asyncio
async def test_plan_resolve_carries_guards():
    client, sent = _client_capturing({"resolved": True, "reason": "resolved"})
    out = await client.plan_resolve(
        "plan_t1", "revise", feedback="split step 2",
        expected_revision=3, decision_id="d-1",
    )
    assert sent["method"] == "plan.resolve"
    assert sent["params"] == {
        "planId": "plan_t1",
        "decision": "revise",
        "feedback": "split step 2",
        "expectedRevision": 3,
        "decisionId": "d-1",
    }
    assert out["resolved"] is True


@pytest.mark.asyncio
async def test_chat_inflight_params():
    client, sent = _client_capturing({"inflight": None, "plan": None})
    reply = await client.chat_inflight("tui:default")
    assert sent["method"] == "chat.inflight"
    assert sent["params"] == {"sessionKey": "tui:default"}
    assert reply == {"inflight": None, "plan": None}


# ── steps text (markup-free rendering) ──────────────────────────────────


def test_plan_steps_text_is_plain_text_with_markers():
    t = _plan_steps_text(_plan())
    assert isinstance(t, Text)
    assert "✓" in t.plain and "›" in t.plain and "·" in t.plain
    # Hostile bracket content survives verbatim (never parsed as markup).
    assert "[/dim]" in t.plain


def test_plan_steps_text_windows_long_plans():
    plan = _plan()
    plan["steps"] = [
        {"id": i, "content": f"step {i}", "status": "completed" if i < 9 else "pending"}
        for i in range(1, 15)
    ]
    t = _plan_steps_text(plan, max_rows=6)
    assert "earlier step" in t.plain
    assert "more" in t.plain


# ── widgets in a live app ───────────────────────────────────────────────


class _Host(App):
    def compose(self) -> ComposeResult:
        yield PlanPanel(id="composer-plan-panel")
        yield PlanApprovalPrompt(id="composer-plan-approval")


@pytest.mark.asyncio
async def test_plan_panel_renders_and_hides():
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.set_plan(_plan(status="executing"))
        await pilot.pause()
        assert panel.has_class("has-plan")
        panel.set_plan(None)
        await pilot.pause()
        assert not panel.has_class("has-plan")


@pytest.mark.asyncio
async def test_plan_panel_revision_guard_drops_stale():
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.set_plan(_plan(status="executing", revision=5))
        await pilot.pause()
        newer = panel._revision
        stale = _plan(status="executing", revision=4)
        stale["title"] = "STALE"
        panel.set_plan(stale)
        await pilot.pause()
        assert panel._revision == newer  # stale snapshot ignored


@pytest.mark.asyncio
async def test_tray_approve_posts_decision():
    decisions: list[tuple[str, str]] = []

    class _Catcher(_Host):
        def on_plan_approval_prompt_decision(
            self, event: PlanApprovalPrompt.Decision
        ) -> None:
            decisions.append((event.decision, event.feedback))

    app = _Catcher()
    async with app.run_test() as pilot:
        tray = app.query_one(PlanApprovalPrompt)
        tray.set_plan(_plan())
        await pilot.pause()
        assert tray.route_editor_key("a") is True  # approve shortcut
        await pilot.pause()
    assert decisions == [("approve", "")]


@pytest.mark.asyncio
async def test_tray_revise_flow_submits_feedback():
    decisions: list[tuple[str, str]] = []

    class _Catcher(_Host):
        def on_plan_approval_prompt_decision(
            self, event: PlanApprovalPrompt.Decision
        ) -> None:
            decisions.append((event.decision, event.feedback))

    app = _Catcher()
    async with app.run_test() as pilot:
        tray = app.query_one(PlanApprovalPrompt)
        tray.set_plan(_plan())
        await pilot.pause()
        tray.route_editor_key("r")  # enter feedback mode
        await pilot.pause()
        from textual.widgets import Input

        field = tray.query_one("#plan-approval-feedback", Input)
        field.value = "add tests"
        field.post_message(Input.Submitted(field, field.value))
        await pilot.pause()
    assert decisions == [("revise", "add tests")]


@pytest.mark.asyncio
async def test_tray_escape_dismisses_without_decision():
    dismissed: list[bool] = []
    decisions: list[str] = []

    class _Catcher(_Host):
        def on_plan_approval_prompt_dismissed(
            self, event: PlanApprovalPrompt.Dismissed
        ) -> None:
            dismissed.append(True)

        def on_plan_approval_prompt_decision(
            self, event: PlanApprovalPrompt.Decision
        ) -> None:
            decisions.append(event.decision)

    app = _Catcher()
    async with app.run_test() as pilot:
        tray = app.query_one(PlanApprovalPrompt)
        tray.set_plan(_plan())
        await pilot.pause()
        assert tray.route_editor_key("escape") is True
        await pilot.pause()
    assert dismissed == [True]
    assert decisions == []


# ── markup-crash regression (the original bug) ─────────────────────────


@pytest.mark.asyncio
async def test_tool_detail_with_hostile_markup_does_not_crash():
    """Regression: expanding a plan/tool detail whose content contains
    bracket sequences (e.g. a bare ``[/dim]``) used to raise MarkupError in
    the compositor and take the whole TUI down."""
    from textual.containers import Vertical

    from flowly.tui.panes.transcript import Bubble

    class _TApp(App):
        def compose(self) -> ComposeResult:
            yield Vertical(Bubble("assistant"))

    app = _TApp()
    async with app.run_test() as pilot:
        bubble = app.query_one(Bubble)
        line = bubble.add_tool(
            "call_x", "plan",
            {"action": "propose", "goal": HOSTILE, "steps": [{"content": HOSTILE}]},
        )
        await pilot.pause()
        line._toggle_expand()          # mounts the detail Static (crash site)
        await pilot.pause()
        line.complete(True, 1200, preview='{"note": "' + HOSTILE + '"}')
        await pilot.pause()
        line._toggle_expand()          # collapse
        line._toggle_expand()          # re-expand with done preview
        await pilot.pause()            # a render pass — must not raise


@pytest.mark.asyncio
async def test_system_bubble_with_bad_markup_degrades_gracefully():
    from textual.containers import Vertical

    from flowly.tui.panes.transcript import Bubble

    class _TApp(App):
        def compose(self) -> ComposeResult:
            yield Vertical(Bubble("system"))

    app = _TApp()
    async with app.run_test() as pilot:
        bubble = app.query_one(Bubble)
        bubble.update_text(f"gateway said: {HOSTILE}")
        await pilot.pause()  # renders plain instead of crashing


@pytest.mark.asyncio
async def test_plan_mode_client_params():
    client, sent = _client_capturing({"sticky": True})
    on = await client.plan_mode_set("tui:default", True)
    assert sent["method"] == "plan.mode.set"
    assert sent["params"] == {"sessionKey": "tui:default", "sticky": True}
    assert on is True
    client2, sent2 = _client_capturing({"sticky": False})
    off = await client2.plan_mode_get("tui:default")
    assert sent2["method"] == "plan.mode.get"
    assert off is False


def test_permission_levels_include_plan_and_matcher_skips_it():
    from flowly.tui.app import _PERMISSION_LEVELS, _match_permission_level

    keys = [lv[0] for lv in _PERMISSION_LEVELS]
    assert keys == ["ask", "auto", "yolo", "plan"]
    # The plan level carries no exec policy and must never match one.
    assert _match_permission_level({"security": "full", "ask": "off"}) == 2
    assert _match_permission_level({"security": "x", "ask": "y"}) == -1


def test_permission_badge_has_plan_style():
    from flowly.tui.panes.status import _PERMISSION_BADGE

    assert "plan" in _PERMISSION_BADGE
