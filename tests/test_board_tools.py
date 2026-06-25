"""Tests for the board agent tools.

Isolation: every test injects a BoardStore backed by ``tmp_path``. The tools
never resolve ``get_flowly_home()`` themselves, so the real ``~/.flowly`` is
never touched.
"""

from __future__ import annotations

import json

import pytest

import asyncio

from flowly.agent.tools.board import (
    BoardAddTool,
    BoardGetTool,
    BoardListTool,
    BoardRunTool,
    BoardUpdateTool,
    build_board_tools,
)
from flowly.board.store import BoardStore, STATUS_DONE, STATUS_IN_PROGRESS


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    yield s
    s.close()


async def _run(tool, **kwargs):
    return json.loads(await tool.execute(**kwargs))


@pytest.mark.asyncio
async def test_add_captures_origin_from_context(store):
    tool = BoardAddTool(store)
    tool.set_context("telegram", "12345")
    res = await _run(tool, title="pay the invoice")
    assert res["ok"] is True
    card = res["card"]
    assert card["title"] == "pay the invoice"
    assert card["originChannel"] == "telegram"
    assert card["originChatId"] == "12345"


@pytest.mark.asyncio
async def test_add_rejects_empty_title(store):
    tool = BoardAddTool(store)
    res = await _run(tool, title="   ")
    assert res["ok"] is False
    assert "title" in res["error"]


@pytest.mark.asyncio
async def test_list_filters_by_status(store):
    add = BoardAddTool(store)
    add.set_context("cli", "direct")
    await _run(add, title="a")
    b = (await _run(add, title="b"))["card"]
    store.set_status(b["id"], STATUS_DONE)

    lst = BoardListTool(store)
    all_cards = await _run(lst)
    assert all_cards["count"] == 2
    todos = await _run(lst, status="todo")
    assert {c["title"] for c in todos["cards"]} == {"a"}


@pytest.mark.asyncio
async def test_get_returns_notes(store):
    card = store.add_card("task")
    store.add_note(card.id, "user", "a note")
    tool = BoardGetTool(store)
    res = await _run(tool, card_id=card.id)
    assert res["ok"] is True
    assert res["card"]["notes"][0]["text"] == "a note"


@pytest.mark.asyncio
async def test_get_missing(store):
    tool = BoardGetTool(store)
    res = await _run(tool, card_id="c_nope")
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_update_moves_and_notes(store):
    card = store.add_card("task")
    tool = BoardUpdateTool(store)
    res = await _run(
        tool, card_id=card.id, status=STATUS_IN_PROGRESS, note="starting"
    )
    assert res["ok"] is True
    assert res["card"]["status"] == STATUS_IN_PROGRESS
    assert any(n["text"] == "starting" for n in res["card"]["notes"])


@pytest.mark.asyncio
async def test_update_done_with_result(store):
    card = store.add_card("task")
    tool = BoardUpdateTool(store)
    res = await _run(tool, card_id=card.id, status=STATUS_DONE, result="shipped")
    assert res["card"]["status"] == STATUS_DONE
    assert res["card"]["result"] == "shipped"


@pytest.mark.asyncio
async def test_update_missing_card(store):
    tool = BoardUpdateTool(store)
    res = await _run(tool, card_id="c_nope", status=STATUS_DONE)
    assert res["ok"] is False


def test_build_board_tools_set(store):
    tools = build_board_tools(store)
    names = {t.name for t in tools}
    assert names == {"board_add", "board_list", "board_get", "board_update"}
    # all share the same store instance
    assert all(t._store is store for t in tools)


def test_build_includes_run_only_with_orchestrator(store):
    assert "board_run" not in {t.name for t in build_board_tools(store)}
    fake = _FakeOrch()
    assert "board_run" in {t.name for t in build_board_tools(store, fake)}


class _FakeOrch:
    def __init__(self):
        self.calls: list = []

    async def run_card(self, card_id):
        self.calls.append(("card", card_id))

    async def run_goal(self, goal, subtasks, *, origin_channel="", origin_chat_id=""):
        self.calls.append(("goal", goal, list(subtasks), origin_channel, origin_chat_id))

    async def cancel_card(self, card_id):
        self.calls.append(("cancel", card_id))
        return True


@pytest.mark.asyncio
async def test_run_single_card(store):
    card = store.add_card("task")
    orch = _FakeOrch()
    tool = BoardRunTool(store, orch)
    res = await _run(tool, card_id=card.id)
    assert res["ok"] is True and res["mode"] == "single"
    await asyncio.sleep(0)  # let the backgrounded coro run
    assert orch.calls == [("card", card.id)]


@pytest.mark.asyncio
async def test_run_parallel_goal_uses_context(store):
    orch = _FakeOrch()
    tool = BoardRunTool(store, orch)
    tool.set_context("telegram", "55")
    res = await _run(tool, goal="ship it", subtasks=["a", "b", "c"])
    assert res["ok"] is True and res["mode"] == "parallel" and res["subtasks"] == 3
    await asyncio.sleep(0)
    assert orch.calls[0][0] == "goal"
    assert orch.calls[0][2] == ["a", "b", "c"]
    assert orch.calls[0][3] == "telegram" and orch.calls[0][4] == "55"


@pytest.mark.asyncio
async def test_run_rejects_both_modes(store):
    card = store.add_card("task")
    tool = BoardRunTool(store, _FakeOrch())
    res = await _run(tool, card_id=card.id, goal="x", subtasks=["y"])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_run_rejects_missing_card(store):
    tool = BoardRunTool(store, _FakeOrch())
    res = await _run(tool, card_id="c_nope")
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_run_refused_in_subagent(store):
    card = store.add_card("task")
    orch = _FakeOrch()
    tool = BoardRunTool(store, orch)
    tool.set_context("telegram", "1", is_subagent=True)
    res = await _run(tool, card_id=card.id)
    assert res["ok"] is False
    assert "subagent" in res["error"]
    await asyncio.sleep(0)
    assert orch.calls == []  # nothing scheduled


@pytest.mark.asyncio
async def test_run_needs_args(store):
    tool = BoardRunTool(store, _FakeOrch())
    res = await _run(tool)
    assert res["ok"] is False
