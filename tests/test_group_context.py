"""Faz B — passive group context buffer (loop side).

The two AgentLoop helpers are exercised as unbound methods against a real
``Session`` dataclass and a stub session manager, so no provider, gateway, or
disk is involved. Covers: observation recording + caps, block rendering with
channel/member header, one-shot clear, and the empty-buffer no-op.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from flowly.agent.loop import (
    _GROUP_BUFFER_MAX_CHARS,
    _GROUP_BUFFER_MAX_MSGS,
    AgentLoop,
)
from flowly.bus.events import InboundMessage
from flowly.session.manager import Session


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_self(session: Session) -> SimpleNamespace:
    saved: list[Session] = []
    mgr = SimpleNamespace(
        get_or_create=lambda key: session,
        save=lambda s, **kw: saved.append(s),
    )
    return SimpleNamespace(sessions=mgr, _saved=saved)


def _observe_msg(content: str, sender_name: str = "Ali", top_level: bool = False) -> InboundMessage:
    meta: dict = {"group_observe": True}
    if top_level:
        meta["sender_name"] = sender_name  # Discord shape
    else:
        meta["slack"] = {"sender_name": sender_name}  # Slack shape
    return InboundMessage(
        channel="slack", sender_id="U1", chat_id="C1", content=content, metadata=meta
    )


# ── recording ───────────────────────────────────────────────────────


def test_observation_is_appended_with_speaker():
    session = Session(key="slack:C1")
    fake = _fake_self(session)
    _run(AgentLoop._record_group_observation(fake, _observe_msg("deploy patladı", "Ali")))
    buf = session.metadata["group_buffer"]
    assert buf == [{"s": "Ali", "c": "deploy patladı"}]
    assert fake._saved, "observation must be persisted"


def test_observation_reads_top_level_sender_name_for_discord():
    session = Session(key="discord:CH1")
    fake = _fake_self(session)
    _run(AgentLoop._record_group_observation(fake, _observe_msg("selam", "Ayşe", top_level=True)))
    assert session.metadata["group_buffer"][0]["s"] == "Ayşe"


def test_observation_falls_back_to_sender_id():
    session = Session(key="slack:C1")
    fake = _fake_self(session)
    msg = InboundMessage(
        channel="slack", sender_id="U9", chat_id="C1", content="x",
        metadata={"group_observe": True},
    )
    _run(AgentLoop._record_group_observation(fake, msg))
    assert session.metadata["group_buffer"][0]["s"] == "U9"


def test_buffer_is_capped_by_message_count():
    session = Session(key="slack:C1")
    fake = _fake_self(session)
    for i in range(_GROUP_BUFFER_MAX_MSGS + 15):
        _run(AgentLoop._record_group_observation(fake, _observe_msg(f"m{i}")))
    buf = session.metadata["group_buffer"]
    assert len(buf) == _GROUP_BUFFER_MAX_MSGS
    # Oldest dropped: the last message is retained.
    assert buf[-1]["c"] == f"m{_GROUP_BUFFER_MAX_MSGS + 14}"


def test_buffer_is_capped_by_total_chars():
    session = Session(key="slack:C1")
    fake = _fake_self(session)
    big = "x" * 1000
    for _ in range(10):  # 10_000 chars > 6_000 cap
        _run(AgentLoop._record_group_observation(fake, _observe_msg(big)))
    total = sum(len(e["c"]) for e in session.metadata["group_buffer"])
    assert total <= _GROUP_BUFFER_MAX_CHARS


# ── flushing / rendering ────────────────────────────────────────────


def _mention_msg(channel_name: str = "", members: list[str] | None = None) -> InboundMessage:
    slack: dict = {"channel_type": "channel"}
    if channel_name:
        slack["channel_name"] = channel_name
    if members:
        slack["members"] = members
    return InboundMessage(
        channel="slack", sender_id="U1", chat_id="C1", content="@bot özetle",
        metadata={"slack": slack},
    )


def test_flush_renders_block_and_clears_buffer():
    session = Session(key="slack:C1")
    session.metadata["group_buffer"] = [
        {"s": "Ali", "c": "deploy patladı"},
        {"s": "Ayşe", "c": "staging'de mi?"},
    ]
    fake = _fake_self(session)
    block = AgentLoop._flush_group_context(fake, session, _mention_msg())
    assert "[Ali]: deploy patladı" in block
    assert "[Ayşe]: staging'de mi?" in block
    assert "did not reply" in block
    # One-shot: buffer emptied and the clear persisted.
    assert session.metadata["group_buffer"] == []
    assert fake._saved


def test_flush_includes_channel_and_members_header():
    session = Session(key="slack:C1")
    session.metadata["group_buffer"] = [{"s": "Ali", "c": "x"}]
    fake = _fake_self(session)
    block = AgentLoop._flush_group_context(
        fake, session, _mention_msg(channel_name="genel", members=["Ali", "Ayşe"])
    )
    assert "Channel: #genel" in block
    assert "Members: Ali, Ayşe" in block


def test_flush_returns_empty_when_no_buffer():
    session = Session(key="slack:C1")
    fake = _fake_self(session)
    assert AgentLoop._flush_group_context(fake, session, _mention_msg()) == ""
    assert not fake._saved  # nothing to clear, nothing saved
