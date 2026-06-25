"""Relay/web chats run concurrently; other channels stay sequential.

The agent loop consumes the bus one message at a time. Processing each turn
inline serialized unrelated relay conversations — chat B's reply could not
start until chat A's full turn (LLM + tools + stream) completed, because both
ride the bus. ``_dispatch_inbound`` now spawns web turns as their own tasks
(like the direct gateway's ``process_direct``) while keeping every other
channel — and system messages (subagent announces) — strictly sequential.
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage


class _FakeBus:
    def __init__(self) -> None:
        self.outbound: list = []

    async def publish_outbound(self, msg) -> None:
        self.outbound.append(msg)


def _bare_loop(process_impl):
    """An AgentLoop stub with just enough wired to exercise dispatch."""
    loop = object.__new__(AgentLoop)
    loop.bus = _FakeBus()
    loop._concurrent_turns = set()
    loop._process_message = process_impl

    async def _noop_state(_state: str) -> None:
        return None

    loop._notify_agent_state = _noop_state
    return loop


def _msg(channel: str, chat_id: str) -> InboundMessage:
    return InboundMessage(
        channel=channel, sender_id="user", chat_id=chat_id, content="hi"
    )


@pytest.mark.asyncio
async def test_web_turn_finishes_inflight_at_run_completion() -> None:
    """The in-flight partial must survive the chat.send publish and only be
    finished when the AGENT LOOP completes the turn.

    Regression: the WebChannel used to finish() in the chat.send task's
    done-callback, but that task merely publishes to the bus and returns
    instantly — so the entry was dropped milliseconds after begin(), before the
    run started, and chat.inflight returned null for the whole tool phase. The
    loop now finishes it here, at true completion.
    """
    from flowly.agent import inflight
    from flowly.channels.web import _WebInboundMessage

    sk = "web:convZZZ"
    inflight.begin(sk, "run-1", "hi")
    assert inflight.get(sk) is not None  # alive through the run

    loop = _bare_loop(lambda _msg: None)  # _process_message returns no response
    msg = _WebInboundMessage(
        channel="web", sender_id="user", chat_id="ws-sid", content="hi",
        metadata={"run_id": "run-1"}, _session_key=sk,
    )
    # During processing the entry is still live; only the finally-block settles it.
    await loop._process_turn(msg)
    assert inflight.get(sk) is None  # finished exactly once, at completion


@pytest.mark.asyncio
async def test_non_web_turn_leaves_inflight_untouched() -> None:
    from flowly.agent import inflight

    sk = "web:other"
    inflight.begin(sk, "run-2", "hi")
    loop = _bare_loop(lambda _msg: None)
    await loop._process_turn(_msg("telegram", "tg"))
    # A telegram turn must not touch a web session's in-flight entry.
    assert inflight.get(sk) is not None
    inflight.finish(sk, "run-2")  # cleanup


@pytest.mark.asyncio
async def test_web_turns_run_concurrently() -> None:
    order: list[tuple[str, str]] = []

    async def slow_process(msg):
        order.append(("start", msg.chat_id))
        await asyncio.sleep(0.05)
        order.append(("finish", msg.chat_id))
        return None

    loop = _bare_loop(slow_process)

    # Dispatch two independent relay chats back to back.
    await loop._dispatch_inbound(_msg("web", "A"))
    await loop._dispatch_inbound(_msg("web", "B"))

    # Dispatch returned immediately for both — neither finished yet.
    await asyncio.sleep(0.01)
    assert order == [("start", "A"), ("start", "B")], (
        "both web turns must start before either finishes (concurrent)"
    )

    # Drain the spawned tasks.
    await asyncio.gather(*list(loop._concurrent_turns))
    assert ("finish", "A") in order and ("finish", "B") in order
    assert loop._concurrent_turns == set()  # done callback discarded them


@pytest.mark.asyncio
async def test_non_web_turn_is_sequential() -> None:
    order: list[tuple[str, str]] = []

    async def slow_process(msg):
        order.append(("start", msg.chat_id))
        await asyncio.sleep(0.02)
        order.append(("finish", msg.chat_id))
        return None

    loop = _bare_loop(slow_process)

    # A Telegram turn must fully complete before dispatch returns — the loop
    # relies on this to keep non-web channels ordered.
    await loop._dispatch_inbound(_msg("telegram", "tg"))
    assert order == [("start", "tg"), ("finish", "tg")]
    assert loop._concurrent_turns == set()  # never spawned a task


@pytest.mark.asyncio
async def test_turn_error_publishes_fallback_and_does_not_raise() -> None:
    async def boom(msg):
        raise RuntimeError("provider exploded")

    loop = _bare_loop(boom)

    # Must not propagate — a crashing web turn can't take down the loop.
    await loop._dispatch_inbound(_msg("web", "C"))
    await asyncio.gather(*list(loop._concurrent_turns))

    assert len(loop.bus.outbound) == 1
    assert "internal error" in loop.bus.outbound[0].content.lower()
    assert loop.bus.outbound[0].chat_id == "C"
