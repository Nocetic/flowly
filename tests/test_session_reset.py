"""``/clear`` must actually clear the conversation.

``Session.clear()`` empties the message list and nothing else. The compaction
summary does not live in that list — it lives in
``metadata['last_compaction_summary']`` and is re-injected at the head of every
later turn by the summary anchor, precisely so it survives the sliding history
window. So after ``/clear`` the transcript looked empty while the model kept
being told everything the previous conversation had established.

Every raw-text surface (Desktop, Web, iOS, the messaging channels) reaches the
same command path, so this was not a niche CLI issue.
"""

from __future__ import annotations

from flowly.agent.loop import AgentLoop
from flowly.compaction.service import CompactionService
from flowly.compaction.types import SUMMARY_MARKER, CompactionConfig
from flowly.session.manager import Session


class _Anchor:
    """Just the surface ``_history_with_summary_anchor`` touches."""

    context_messages = 100
    _history_with_summary_anchor = AgentLoop._history_with_summary_anchor


def _compacted_session() -> Session:
    session = Session(key="web:conv-1")
    session.metadata["last_compaction_summary"] = "OLD SUMMARY MUST BE GONE"
    session.metadata["compaction_count"] = 3
    session.metadata["group_buffer"] = [{"role": "user", "content": "group chatter"}]
    session.metadata["persona"] = "default"
    session.metadata["title"] = "Deploy debugging"
    session.add_message("system", f"{SUMMARY_MARKER}\n\nOLD SUMMARY MUST BE GONE")
    session.add_message("user", "and then?")
    return session


def test_plain_clear_leaves_the_summary_behind():
    """Pinned as the reason reset_conversation_context exists — if this ever
    stops being true, the two methods have converged and one should go."""
    session = _compacted_session()

    session.clear()

    assert session.metadata.get("last_compaction_summary")


def test_reset_drops_everything_the_model_would_be_told():
    session = _compacted_session()

    session.reset_conversation_context()

    assert session.messages == []
    history = _Anchor()._history_with_summary_anchor(session)
    assert history == [], (
        f"the emptied chat still feeds the model: {history}"
    )
    assert "group_buffer" not in session.metadata
    assert "compaction_count" not in session.metadata


def test_reset_keeps_the_conversation_s_identity():
    """A fresh conversation, not a fresh chat: the user's persona, the chat's
    name and its pinned settings are not content."""
    session = _compacted_session()
    session.metadata["cwd"] = "/Users/me/project"
    session.metadata["model_override"] = "anthropic/claude-haiku-4.5"

    session.reset_conversation_context()

    assert session.metadata["persona"] == "default"
    assert session.metadata["title"] == "Deploy debugging"
    assert session.metadata["cwd"] == "/Users/me/project"
    assert session.metadata["model_override"] == "anthropic/claude-haiku-4.5"


def test_unknown_metadata_is_dropped_by_default():
    """An allowlist, deliberately: a key added later that happens to carry
    conversation content must not leak into the next chat because nobody
    remembered to add it to a denylist."""
    session = _compacted_session()
    session.metadata["some_future_context_cache"] = ["everything the user said"]

    session.reset_conversation_context()

    assert "some_future_context_cache" not in session.metadata


def test_the_compaction_service_forgets_the_session_too():
    """Its counters describe history that no longer exists; carried over, a
    failure cooldown would suppress the new chat's first real compaction."""
    class _P:
        provider_name = "stub"

    service = CompactionService(provider=_P(), model="m", config=CompactionConfig())
    service.record_compaction_failure("web:conv-1")
    service.record_compaction_failure("web:conv-1")
    assert service._state("web:conv-1").consecutive_failures == 2

    service.reset_session("web:conv-1")

    assert service._state("web:conv-1").consecutive_failures == 0


async def test_reset_never_evicts_a_session_mid_compaction():
    class _P:
        provider_name = "stub"

    service = CompactionService(provider=_P(), model="m", config=CompactionConfig())
    async with service.session_lock("web:conv-1"):
        service.record_compaction_failure("web:conv-1")
        service.reset_session("web:conv-1")

        assert "web:conv-1" in service._sessions, (
            "dropping a locked session hands the next caller a fresh lock and "
            "silently undoes the exclusion an in-flight commit depends on"
        )
        assert service._sessions["web:conv-1"].consecutive_failures == 0, (
            "the lock belongs to the in-flight operation, but its breaker "
            "must not leak into the freshly reset conversation"
        )
