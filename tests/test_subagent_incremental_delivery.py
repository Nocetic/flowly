"""Incremental delivery of async subagent results.

Two mechanisms keep multi-subagent fan-out responsive — the parent answers
each subagent AS it finishes instead of marathoning and dumping everything at
the end:

  * Ayak A — after an async (background) subagent is dispatched, the parent's
    tool policy hides ALL tools, so the next model step is a plain ack and the
    turn ends. The parent then goes idle and can process each completion
    announce promptly. (_apply_turn_tool_policy)
  * Ayak B — the announce queue delivers each completion as its OWN message
    (FIFO), never merged into a single "N tasks completed" summary, so each
    finished subagent gets its own reply. (AnnounceQueue._drain)
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.subagent_announce_queue import AnnounceItem, AnnounceQueue

# ── Ayak A: the parent keeps its tools after a dispatch ─────────────────────

def _tool_defs(*names: str) -> list[dict]:
    return [{"function": {"name": n}} for n in names]


def test_policy_never_hides_tools_outside_a_live_call() -> None:
    """A dispatch no longer ends the parent's turn by taking its tools away;
    the parent keeps working while its subagents run."""
    agent = object.__new__(AgentLoop)  # bypass heavy __init__
    defs = _tool_defs("web_search", "exec", "spawn", "message", "artifact")
    filtered, hidden = agent._apply_turn_tool_policy(defs, live_call_turn=False)
    assert filtered == defs
    assert hidden == []


# ── Ayak B: announce queue delivers each completion separately ──────────────

async def _drain_until(sent: list, n: int, timeout: float = 2.0) -> None:
    waited = 0.0
    while len(sent) < n and waited < timeout:
        await asyncio.sleep(0.01)
        waited += 0.01


@pytest.mark.asyncio
async def test_announce_queue_delivers_each_separately() -> None:
    sent: list[str] = []

    async def send_fn(prompt: str) -> None:
        sent.append(prompt)

    q = AnnounceQueue("web:conv-1", send_fn)
    await q.enqueue(AnnounceItem(prompt="result A", summary="A — done"))
    await q.enqueue(AnnounceItem(prompt="result B", summary="B — done"))
    await q.enqueue(AnnounceItem(prompt="result C", summary="C — done"))

    await _drain_until(sent, 3)

    # One message per completion, in order — never a merged batch summary.
    assert sent == ["result A", "result B", "result C"]
    assert all("background tasks completed" not in s for s in sent)


@pytest.mark.asyncio
async def test_announce_queue_preserves_late_arrivals() -> None:
    sent: list[str] = []

    async def send_fn(prompt: str) -> None:
        sent.append(prompt)
        await asyncio.sleep(0.01)  # simulate delivery latency

    q = AnnounceQueue("web:conv-2", send_fn)
    await q.enqueue(AnnounceItem(prompt="first", summary="1"))
    # Arrives while the first is still being delivered (drain in flight).
    await q.enqueue(AnnounceItem(prompt="second", summary="2"))

    await _drain_until(sent, 2)
    assert sent == ["first", "second"]
