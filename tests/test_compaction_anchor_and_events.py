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
