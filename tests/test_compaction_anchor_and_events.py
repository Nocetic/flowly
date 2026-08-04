"""The summary must outlive the history window, and events must be readable.

Two separate regressions live here:

  * A compaction summary used to be an ordinary message inside the sliding
    ``get_history`` window. After ``context_messages`` further messages it
    slid out, and the model lost every turn the summary was protecting —
    invisibly, because the chat UI reads a different (full) transcript.
  * The gateway sends ``tokensBefore`` / ``tokensAfter`` / ``messagesRemoved``
    while the TUI read ``beforeTokens`` / ``afterTokens`` / ``beforeMessages``,
    so every compaction rendered as "0→0 msgs".
"""

import pytest

from flowly.compaction.types import SUMMARY_MARKER, is_summary_message
from flowly.session.manager import Session
from flowly.tui.client import CompactionEvent, GatewayClient


@pytest.fixture
def temp_flowly_home(tmp_path, monkeypatch):
    """Redirect FLOWLY_HOME so SessionManager writes into tmp_path."""
    home = tmp_path / "flowly-home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile
    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    return home


class _Loop:
    """The two AgentLoop members ``_history_with_summary_anchor`` touches."""

    context_messages = 100

    from flowly.agent.loop import AgentLoop

    _history_with_summary_anchor = AgentLoop._history_with_summary_anchor


def _session_with_summary(extra_messages: int) -> Session:
    session = Session(key="cli:test")
    session.metadata["last_compaction_summary"] = "The user is migrating a database."
    session.add_message("system", f"{SUMMARY_MARKER}\n\nThe user is migrating a database.")
    for i in range(extra_messages):
        session.add_message("user", f"m{i}")
        session.add_message("assistant", f"r{i}")
    return session


# ── Summary survives the sliding window ───────────────────────────────────


def test_summary_present_right_after_compaction():
    history = _Loop()._history_with_summary_anchor(_session_with_summary(2))

    assert is_summary_message(history[0])
    # Not duplicated: the real message is already there.
    assert sum(1 for m in history if is_summary_message(m)) == 1


def test_summary_survives_far_past_the_history_window():
    # 400 messages — four times the 100-message window that used to drop it.
    session = _session_with_summary(200)

    history = _Loop()._history_with_summary_anchor(session)

    assert is_summary_message(history[0]), (
        "compaction summary slid out of the window; the model lost all "
        "pre-compaction context while the UI still showed it"
    )
    assert "migrating a database" in history[0]["content"]
    assert sum(1 for m in history if is_summary_message(m)) == 1


def test_no_anchor_when_session_was_never_compacted():
    session = Session(key="cli:test")
    session.add_message("user", "hello")

    history = _Loop()._history_with_summary_anchor(session)

    assert not any(is_summary_message(m) for m in history)
    assert len(history) == 1


def test_anchor_is_dropped_once_the_stored_summary_is_gone():
    session = _session_with_summary(200)
    session.metadata.pop("last_compaction_summary")

    history = _Loop()._history_with_summary_anchor(session)

    assert not any(is_summary_message(m) for m in history)


# ── Event contract matches the producer ───────────────────────────────────


def _parse_event(payload: dict) -> CompactionEvent:
    """Run one real gateway event frame through the TUI client's dispatcher."""
    import asyncio

    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()

    async def run():
        await GatewayClient._dispatch(
            client, {"type": "event", "event": "compaction", "data": payload},
        )
        return await client._inbox.get()

    return asyncio.run(run())


def test_tui_reads_the_fields_the_gateway_actually_sends():
    # Exactly the payload built in gateway_cmd._on_auto_compaction.
    event = _parse_event({
        "phase": "completed",
        "tokensBefore": 96_000,
        "tokensAfter": 12_500,
        "messagesRemoved": 42,
        "sessionKey": "cli:default",
    })

    assert isinstance(event, CompactionEvent)
    assert event.phase == "completed"
    assert event.before_tokens == 96_000
    assert event.after_tokens == 12_500
    assert event.messages_removed == 42
    assert event.session_key == "cli:default"


@pytest.mark.parametrize("phase", ["started", "completed", "failed"])
def test_every_lifecycle_phase_is_carried_through(phase):
    event = _parse_event({"phase": phase, "sessionKey": "s"})

    assert event.phase == phase


def test_snake_case_payloads_still_parse():
    event = _parse_event({
        "phase": "completed",
        "tokens_before": 10,
        "tokens_after": 4,
        "messages_removed": 2,
    })

    assert (event.before_tokens, event.after_tokens, event.messages_removed) == (10, 4, 2)


def test_missing_counts_degrade_to_zero_not_crash():
    event = _parse_event({"phase": "started"})

    assert event.before_tokens == 0
    assert event.messages_removed == 0
    assert event.session_key == ""


def test_anchor_keeps_an_approved_plan_in_context():
    """The plan note is baked into the summary MESSAGE, not the stored text.
    When that message slides out, the anchor must carry the plan forward."""
    from flowly.plans.manager import get_plan_manager

    session = _session_with_summary(200)
    manager = get_plan_manager()
    note = "[ACTIVE PLAN] step 1 pending"

    original = manager.compaction_note
    manager.compaction_note = lambda key: note
    try:
        history = _Loop()._history_with_summary_anchor(session)
    finally:
        manager.compaction_note = original

    assert note in history[0]["content"], (
        "an approved plan vanished from context when the summary slid out"
    )


def test_anchor_survives_a_broken_plan_manager():
    from flowly.plans.manager import get_plan_manager

    session = _session_with_summary(200)
    manager = get_plan_manager()

    def _boom(key):
        raise RuntimeError("plan store unavailable")

    original = manager.compaction_note
    manager.compaction_note = _boom
    try:
        history = _Loop()._history_with_summary_anchor(session)
    finally:
        manager.compaction_note = original

    assert is_summary_message(history[0]), "plan failure must not drop the summary"


def test_relay_compaction_event_carries_the_session_key():
    """The relay routes conversation-scoped events by data.sessionKey (as it
    does for plan.*). Without it only the origin socket is reachable, so a
    second device viewing the same chat never learns about the compaction."""
    import asyncio
    import json

    from flowly.channels.web import WebChannel

    sent: list[str] = []

    class _WS:
        async def send(self, payload):
            sent.append(payload)

    channel = WebChannel.__new__(WebChannel)
    channel._ws = _WS()
    channel._session_key_to_relay_id = {"web:chat-1": "relay-abc"}

    asyncio.run(
        WebChannel.send_compaction_event(
            channel, "web:chat-1", 90_000, 10_000, 30, "completed",
        )
    )

    assert sent, "no event was sent"
    data = json.loads(sent[0])["data"]
    assert data["sessionKey"] == "web:chat-1"
    assert data["phase"] == "completed"
    assert (data["tokensBefore"], data["tokensAfter"], data["messagesRemoved"]) == (
        90_000, 10_000, 30,
    )


def test_summary_message_is_flagged_not_just_prefixed():
    """Detection reads a recorded fact; the text prefix is only a fallback."""
    from flowly.compaction.types import SUMMARY_METADATA_KEY

    flagged = {"role": "system", "content": "anything at all",
               SUMMARY_METADATA_KEY: True}

    assert is_summary_message(flagged)


def test_user_message_quoting_the_marker_is_not_a_summary():
    assert not is_summary_message({"role": "user", "content": SUMMARY_MARKER})


def test_flag_survives_the_session_round_trip_but_never_reaches_the_llm(temp_flowly_home):
    from pathlib import Path

    from flowly.compaction.types import SUMMARY_METADATA_KEY
    from flowly.session.manager import SessionManager

    manager = SessionManager(workspace=Path("/tmp"))
    session = manager.get_or_create("cli:flagtest")
    session.add_message("system", f"{SUMMARY_MARKER}\n\nx", **{SUMMARY_METADATA_KEY: True})
    session.add_message("user", "hi")
    manager.save(session)

    manager._cache.clear()
    reloaded = manager.get_or_create("cli:flagtest")

    raw = [m for m in reloaded.messages if m.get("role") == "system"]
    assert raw and raw[0].get(SUMMARY_METADATA_KEY) is True, "flag must persist"
    assert all(
        SUMMARY_METADATA_KEY not in m for m in reloaded.get_history(max_messages=10)
    ), "flag must never ride out to a provider"


def test_anchor_not_duplicated_when_the_window_holds_a_flagged_summary():
    from flowly.compaction.types import SUMMARY_METADATA_KEY

    session = Session(key="cli:test")
    session.metadata["last_compaction_summary"] = "prior work"
    session.add_message("system", "prior work summary", **{SUMMARY_METADATA_KEY: True})
    session.add_message("user", "next")

    history = _Loop()._history_with_summary_anchor(session)

    assert sum(1 for m in history if is_summary_message(m)) <= 1
