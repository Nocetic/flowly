"""Conformance tests for the chat wire protocol.

The contract these pin is written down in ``docs/chat-wire-protocol.md``.
It exists because the rules used to live only as assumptions spread across
four repositories — bot, relay, desktop, iOS — and a single new message type
that violated one of them broke three clients in three different ways at once
(the `[context-optimized]` separator arriving as a `state:"final"` event).

These are PRODUCER-side assertions: what the bot actually puts on the wire.
The client repos carry the mirror assertions (desktop:
``compaction-event.test.ts``; relay: ``stream-routing.test.ts``). Together
they are the two ends of one contract — change a shape here without changing
them and this file fails first.
"""

from __future__ import annotations

import json

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig

# The conversation-scoped names the relay fans out to every device viewing a
# chat (CONVERSATION_EVENT_NAMES in stream-routing.ts). Anything NOT in this
# set is request-scoped and must stay on the originating socket.
CONVERSATION_SCOPED_EVENTS = {"chat", "plan.updated", "plan.approval.requested", "compaction"}

# States that END a turn. A consumer of these settles live state, so anything
# that is not a turn terminal must never wear one (§4.2).
TERMINAL_STATES = {"final", "error"}


@pytest.fixture
def channel():
    """A WebChannel whose transport is captured instead of sent."""
    ch = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    capture: list[dict] = []

    async def fake_send_or_queue(payload: str) -> None:
        capture.append(json.loads(payload))

    ch._send_or_queue = fake_send_or_queue  # type: ignore[method-assign]
    ch._capture = capture
    return ch


def _final(**metadata) -> OutboundMessage:
    return OutboundMessage(
        channel="web", chat_id="relay-sess-1", content="Done.",
        metadata={"run_id": "run-1", **metadata},
    )


# ── §1/§2: envelope and identity ──────────────────────────────────────────


async def test_every_chat_event_carries_its_conversation(channel):
    """§2: without sessionKey the relay can only reach the socket that started
    the turn — a client that reconnected mid-turn (a long compaction is
    enough) never receives the rest of its own reply."""
    await channel.send(_final())
    await channel.send(OutboundMessage(
        channel="web", chat_id="relay-sess-1", content="",
        metadata={"iteration_event": {
            "runId": "run-1", "iterationIdx": 0, "role": "assistant",
            "content": "", "tool_calls": [{"id": "c1"}],
        }},
    ))

    assert channel._capture, "nothing was put on the wire"
    for event in channel._capture:
        assert event["type"] == "event"
        assert event["event"] in CONVERSATION_SCOPED_EVENTS
        data = event["data"]
        assert data.get("sessionKey"), (
            f"{data.get('state')} event has no sessionKey — the relay would "
            "route it to the socket the turn started on"
        )
        assert data.get("runId") is not None


async def test_the_envelope_is_stable(channel):
    await channel.send(_final())

    event = channel._capture[0]
    assert set(event) >= {"type", "event", "data"}
    assert event["event"] == "chat"
    assert event["data"]["state"] == "final"


# ── §3: additive-only fields (mixed-version relays and clients) ────────────


async def test_optional_fields_are_omitted_not_nulled(channel):
    """§6.3: omitting a new field must reproduce exactly the old wire shape.
    A relay that predates the field must see the bytes it already handles."""
    await channel.send(_final())

    data = channel._capture[0]["data"]
    for optional in ("toolMessages", "attachments", "aborted", "failed"):
        assert optional not in data, (
            f"{optional} was emitted as a null/empty rather than omitted"
        )


async def test_an_aborted_turn_is_still_a_final(channel):
    """§4.3: Stop does not replace the terminal; it annotates it. Clients keep
    the partial text until this lands."""
    await channel.send(_final(aborted=True))

    data = channel._capture[0]["data"]
    assert data["state"] == "final"
    assert data["aborted"] is True


async def test_a_provider_error_is_marked_and_not_a_success(channel):
    await channel.send(_final(error={"code": "MODEL_PROVIDER_UNAVAILABLE"}))

    data = channel._capture[0]["data"]
    assert data["failed"] is True, (
        "the relay would persist this as a successful assistant completion "
        "and send an unread push"
    )


# ── §4.2: the compaction separator is not a turn terminal ─────────────────


def test_the_bot_never_puts_the_separator_on_the_wire():
    """The root fix: no reply-shaped message carries the separator any more.

    It used to ride `state:"final"`, so every consumer of turn terminals had
    to recognise the string or it would settle a turn that was still
    streaming — and each new surface had to remember. The transcript boundary
    is written by the transport from the typed `compaction` event instead, so
    there is nothing to recognise and nothing to forget.
    """
    import inspect

    from flowly.agent import loop as loop_module

    source = inspect.getsource(loop_module)
    producing_lines = [
        line for line in source.splitlines()
        if "[context-optimized]" in line and not line.strip().startswith("#")
    ]
    assert not producing_lines, (
        "the bot is emitting the separator again: " + "; ".join(producing_lines)
    )


async def test_a_manual_compact_replies_in_words(channel):
    """The /compact reply IS its turn's terminal. Sending the separator there
    made consumers treat the terminal as "not a terminal", so the run never
    settled and the chat sat on "replying" forever."""
    await channel.send(_final(run_id="compact-1"))
    data = channel._capture[0]["data"]
    text = "".join(b.get("text", "") for b in data["message"]["content"])

    assert text.strip() != "[context-optimized]"


# ── §3/§5: the compaction event ───────────────────────────────────────────


class _FakeSocket:
    """Captures frames written straight to the relay socket."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, payload: str) -> None:
        self.frames.append(json.loads(payload))


def _bind_relay_session(channel, session_key: str = "web:conv-1") -> _FakeSocket:
    socket = _FakeSocket()
    channel._ws = socket
    channel._session_key_to_relay_id[session_key] = "relay-sess-1"
    return socket


async def test_compaction_event_field_names(channel):
    """These exact names are read by the desktop (readCompactionEvent), iOS
    (CompactionState) and the TUI. They were once tokensBefore on the wire and
    beforeTokens in a client, and every compaction rendered as '0→0 msgs'."""
    sent = _bind_relay_session(channel).frames

    await channel.send_compaction_event(
        "web:conv-1", tokens_before=96_000, tokens_after=12_500,
        messages_removed=42, phase="completed",
    )

    assert sent, "compaction event never reached the wire"
    event = sent[0]
    assert event["event"] == "compaction"
    assert event["event"] in CONVERSATION_SCOPED_EVENTS, (
        "the relay must fan compaction out to every device viewing the chat"
    )
    data = event["data"]
    assert data["phase"] == "completed"
    assert data["tokensBefore"] == 96_000
    assert data["tokensAfter"] == 12_500
    assert data["messagesRemoved"] == 42
    assert data["sessionKey"], "§2: no sessionKey means no conversation routing"


@pytest.mark.parametrize("phase", ["started", "completed", "failed"])
async def test_every_compaction_phase_is_deliverable(channel, phase):
    """§5.1: an unclosed `started` leaves the UI shimmering forever, so the
    closing phases must be as deliverable as the opening one."""
    sent = _bind_relay_session(channel).frames

    await channel.send_compaction_event("web:conv-1", 1, 1, 0, phase=phase)

    assert sent[0]["data"]["phase"] == phase


# ── §4.4: re-entry must carry every kind of live state ────────────────────


def test_chat_inflight_returns_the_whole_live_turn():
    """A client that reconnects mid-turn rebuilds from this alone. Anything
    live that is missing here is invisible until the turn ends."""
    from flowly.agent import compaction_status, inflight
    from flowly.channels.feature_rpc import chat_inflight

    session_key = "desktop:conformance"
    inflight.begin(session_key, "run-9", "what changed?")
    inflight.append(session_key, "run-9", "Looking")
    inflight.append_iteration(session_key, "run-9", {
        "runId": "run-9", "iterationIdx": 0, "role": "assistant", "content": "",
    })
    compaction_status.record(session_key, "started", 90_000, 0, 0, now=1_000.0)

    result = chat_inflight({"sessionKey": session_key})

    # Three independent kinds of live state travel in ONE handshake — a client
    # gets the turn back with a single round trip or not at all.
    turn = result["inflight"]
    assert turn["runId"] == "run-9"
    assert turn["text"] == "Looking"
    assert turn["user"] == "what changed?", "the user's own bubble would vanish"
    assert len(turn["iterations"]) == 1, "the live tool panel would be empty"
    assert result["compaction"]["phase"] == "started", (
        "reopening mid-summarisation would show an idle transcript while the "
        "turn is visibly stalled"
    )
    assert "plan" in result, "the plan bar would not survive re-entry"

    # §4.1: the terminal retires the live turn, so a later re-entry restores
    # nothing rather than replaying a finished run.
    inflight.finish(session_key, "run-9")
    assert chat_inflight({"sessionKey": session_key})["inflight"] is None
    compaction_status.clear(session_key)


def test_chat_inflight_is_null_when_nothing_is_running():
    """The keys are always present — a client must be able to read them
    unconditionally rather than probing for their existence."""
    from flowly.channels.feature_rpc import chat_inflight

    result = chat_inflight({"sessionKey": "desktop:idle-session"})

    assert result["inflight"] is None
    assert result["compaction"] is None
    assert result["plan"] is None


def test_a_compaction_cycle_is_identifiable():
    """§5: without an id a client cannot tell whether the `completed` it just
    received closes the `started` it is showing or belongs to another pass —
    it has to guess, and across a reconnect or a second device it guesses
    wrong. The id is also the relay's document key for the boundary row, so a
    duplicated event cannot draw two dividers."""
    from flowly.agent import compaction_status
    from flowly.agent.loop import AgentLoop

    first, second = AgentLoop._new_compaction_id(), AgentLoop._new_compaction_id()
    assert first != second
    assert first.startswith("cmp_")

    compaction_status.clear("s-id")
    compaction_status.record(
        "s-id", "started", 90_000, 0, 0, now=1_000.0, compaction_id=first,
    )
    state = compaction_status.get("s-id", now=1_001.0)

    assert state["compactionId"] == first, (
        "a client restoring through chat.inflight cannot correlate the cycle"
    )
    compaction_status.clear("s-id")


async def test_the_compaction_event_carries_its_cycle_id(channel):
    sent = _bind_relay_session(channel).frames

    await channel.send_compaction_event(
        "web:conv-1", 96_000, 12_500, 42, phase="completed",
        compaction_id="cmp_abc123",
    )

    assert sent[0]["data"]["compactionId"] == "cmp_abc123"


async def test_an_old_notifier_shape_still_works(channel):
    """Additive-only: omitting the id must reproduce the previous wire bytes,
    for a relay or client that predates it."""
    sent = _bind_relay_session(channel).frames

    await channel.send_compaction_event("web:conv-1", 1, 1, 0, phase="started")

    assert "compactionId" not in sent[0]["data"]


# ── The direct gateway is not a second-class surface ──────────────────────


def test_the_boundary_is_persisted_for_transports_without_firestore(tmp_path, monkeypatch):
    """A relay client gets its divider from a Firestore row the relay writes.
    A direct-gateway client (desktop on a local/remote WS, iOS the same) reads
    history from disk instead — so the same boundary has to be written there,
    or a reopened chat gives no hint that anything was ever summarised."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    from flowly.session.manager import SessionManager

    manager = SessionManager(workspace=tmp_path)
    session = manager.get_or_create("desktop:default")
    session.add_message("user", "first question")
    session.add_message("assistant", "first answer")
    manager.flush_full(session)

    manager.append_context_boundary(session, "cmp_deadbeef")

    rows = manager.get_full_messages("desktop:default")
    boundary = [r for r in rows if r.get("kind") == "context_boundary"]
    assert len(boundary) == 1, rows
    assert boundary[0]["compactionId"] == "cmp_deadbeef"
    assert boundary[0]["content"] == "[context-optimized]", (
        "the legacy text must stay: clients that match on it predate `kind`"
    )
    # And the real conversation is still all there, in order.
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
