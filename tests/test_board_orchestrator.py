"""Tests for BoardOrchestrator — execution, parallelism, cancel.

A fake ``spawn_fn`` stands in for SubagentManager.spawn(wait=True): no LLM,
fully deterministic. The orchestrator is the sole board writer; the fake
worker only returns a string, mirroring the production invariant.
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.board.orchestrator import BoardOrchestrator
from flowly.board.store import (
    BoardError,
    BoardStore,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_TODO,
)


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    yield s
    s.close()


def make_spawn(result="ok", *, calls=None):
    async def spawn_fn(task, *, label=None, origin_channel="", origin_chat_id="", model=None):
        if calls is not None:
            calls.append({"task": task, "label": label, "channel": origin_channel})
        return f"{result}: {task[:20]}"
    return spawn_fn


def collect_notify(sink):
    async def notify(channel, chat_id, text):
        sink.append((channel, chat_id, text))
    return notify


@pytest.mark.asyncio
async def test_run_card_success(store):
    notes = []
    card = store.add_card("do thing", origin_channel="telegram", origin_chat_id="7")
    orch = BoardOrchestrator(store, make_spawn("done"), notify=collect_notify(notes))
    res = await orch.run_card(card.id)
    assert res["ok"] is True
    assert res["outcome"] == "done"
    assert store.get_card(card.id).status == STATUS_DONE
    assert store.get_card(card.id).result.startswith("done:")
    # reported back on the origin channel
    assert notes and notes[0][0] == "telegram" and notes[0][1] == "7"


@pytest.mark.asyncio
async def test_run_card_failure_is_retryable(store):
    async def boom(task, **kw):
        raise RuntimeError("kaboom")

    notes = []
    card = store.add_card("flaky", origin_channel="discord", origin_chat_id="1")
    orch = BoardOrchestrator(store, boom, notify=collect_notify(notes))
    res = await orch.run_card(card.id)
    assert res["ok"] is False
    assert res["outcome"] == "failed"
    fresh = store.get_card(card.id)
    assert fresh.status == STATUS_TODO  # back to todo, retryable
    assert fresh.error == "kaboom"
    assert any("run failed" in n.text for n in fresh.notes)
    assert notes and "failed" in notes[0][2]


@pytest.mark.asyncio
async def test_run_card_guards(store):
    orch = BoardOrchestrator(store, make_spawn())
    with pytest.raises(BoardError):
        await orch.run_card("c_missing")

    done = store.add_card("x")
    store.set_status(done.id, STATUS_DONE)
    with pytest.raises(BoardError):
        await orch.run_card(done.id)


@pytest.mark.asyncio
async def test_run_goal_all_done(store):
    notes = []
    calls = []
    orch = BoardOrchestrator(store, make_spawn("ok", calls=calls), notify=collect_notify(notes))
    res = await orch.run_goal(
        "ship feature", ["write tests", "implement", "review"],
        origin_channel="telegram", origin_chat_id="9",
    )
    assert res["done"] == 3
    assert res["summary"] == "3/3 done"
    # parent + 3 children created
    parent = store.get_card(res["parentId"])
    assert parent.status == STATUS_DONE
    children = store.list_cards(parent_id=parent.id)
    assert len(children) == 3
    assert all(c.status == STATUS_DONE for c in children)
    assert len(calls) == 3
    assert notes and "3/3 done" in notes[0][2]


@pytest.mark.asyncio
async def test_run_goal_notifies_finished_once_for_parent(store):
    finished = []

    async def on_finished(card, outcome):
        finished.append((card.id, card.title, card.result, outcome))

    orch = BoardOrchestrator(store, make_spawn("ok"), on_finished=on_finished)
    res = await orch.run_goal("ship feature", ["write tests", "implement"])

    assert finished == [(res["parentId"], "ship feature", "2/2 done", "done")]


@pytest.mark.asyncio
async def test_run_goal_partial_failure(store):
    async def spawn_fn(task, **kw):
        if "bad" in task:
            raise RuntimeError("nope")
        return "ok"

    orch = BoardOrchestrator(store, spawn_fn)
    res = await orch.run_goal("mix", ["good one", "bad one", "good two"])
    assert res["done"] == 2
    assert res["failed"] == 1
    assert "2/3 done" in res["summary"] and "1 failed" in res["summary"]


@pytest.mark.asyncio
async def test_run_goal_requires_subtasks(store):
    orch = BoardOrchestrator(store, make_spawn())
    with pytest.raises(BoardError):
        await orch.run_goal("empty", [])
    with pytest.raises(BoardError):
        await orch.run_goal("", ["a"])


@pytest.mark.asyncio
async def test_concurrency_cap_enforced(store):
    """No more than MAX_PARALLEL spawns run at once, even with many subtasks."""
    state = {"active": 0, "max": 0}
    release = asyncio.Event()

    async def spawn_fn(task, **kw):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        try:
            await release.wait()
        finally:
            state["active"] -= 1
        return "ok"

    orch = BoardOrchestrator(store, spawn_fn)
    subtasks = [f"task {i}" for i in range(12)]
    goal_task = asyncio.create_task(orch.run_goal("big", subtasks))
    # Let the scheduler admit up to the cap.
    for _ in range(50):
        await asyncio.sleep(0)
        if state["active"] >= orch.MAX_PARALLEL:
            break
    assert state["max"] <= orch.MAX_PARALLEL
    assert state["active"] == orch.MAX_PARALLEL  # cap saturated, rest queued
    release.set()
    res = await goal_task
    assert res["done"] == 12
    assert state["max"] <= orch.MAX_PARALLEL


@pytest.mark.asyncio
async def test_cancel_running_card(store):
    hang = asyncio.Event()

    async def spawn_fn(task, **kw):
        await hang.wait()  # never completes on its own
        return "ok"

    card = store.add_card("long job")
    orch = BoardOrchestrator(store, spawn_fn)
    run = asyncio.create_task(orch.run_card(card.id))
    # wait until it's actually running
    for _ in range(50):
        await asyncio.sleep(0)
        if orch.is_running(card.id):
            break
    assert orch.is_running(card.id)

    cancelled = await orch.cancel_card(card.id)
    assert cancelled is True
    res = await run
    assert res["outcome"] == "cancelled"
    assert store.get_card(card.id).status == STATUS_CANCELLED


@pytest.mark.asyncio
async def test_spawn_fn_never_receives_store(store):
    """Single-writer invariant: the worker boundary gets only task text +
    routing — never a board handle. It cannot write the board."""
    seen_kwargs = {}

    async def spawn_fn(task, **kw):
        seen_kwargs.update(kw)
        return "ok"

    card = store.add_card("t", origin_channel="cli", origin_chat_id="d")
    orch = BoardOrchestrator(store, spawn_fn)
    await orch.run_card(card.id)
    assert set(seen_kwargs) == {"label", "origin_channel", "origin_chat_id", "model"}
    assert "store" not in seen_kwargs
