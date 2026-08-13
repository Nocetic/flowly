"""Encrypted provider checkpoint placement, accounting, and persistence."""

from flowly.agent.loop import AgentLoop
from flowly.compaction.estimator import estimate_messages_tokens
from flowly.providers.base import PROVIDER_COMPACTION_CHECKPOINT_KEY
from flowly.session.archive import EVENT_ID_KEY
from flowly.session.manager import Session


def _marker(value: str = "opaque") -> dict:
    return {
        PROVIDER_COMPACTION_CHECKPOINT_KEY: {
            "type": "compaction",
            "encrypted_content": value,
        }
    }


def test_checkpoint_is_inserted_after_its_exact_archive_event() -> None:
    session = Session(key="web:one")
    session.add_message("user", "first")
    covered_id = session.messages[-1][EVENT_ID_KEY]
    session.add_message("assistant", "answer")
    session.add_message("user", "next")

    history = session.get_history_with_checkpoint(
        covered_event_id=covered_id,
        marker=_marker(),
    )

    assert history is not None
    assert history[0] == {"role": "user", "content": "first"}
    assert PROVIDER_COMPACTION_CHECKPOINT_KEY in history[1]
    assert history[2:] == [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next"},
    ]


def test_checkpoint_budget_replaces_only_earlier_conversation_tokens() -> None:
    old = {"role": "user", "content": "old " * 20_000}
    instructions = {"role": "system", "content": "stable policy"}
    tail = {"role": "user", "content": "current question"}
    without_checkpoint = estimate_messages_tokens([instructions, old, tail])
    with_checkpoint = estimate_messages_tokens([
        instructions,
        old,
        _marker(),
        tail,
    ])

    assert with_checkpoint < without_checkpoint // 10
    assert with_checkpoint >= estimate_messages_tokens([instructions, tail])


def test_new_checkpoint_persists_against_the_input_event_not_the_output() -> None:
    session = Session(key="web:one")
    session.add_message("user", "older")
    turn_start = len(session.messages)
    session.extend_with_turn_messages(
        user_content="current",
        new_messages=[{"role": "assistant", "content": "response"}],
        final_content="response",
    )
    state = {
        "provider": "openai_codex",
        "model": "gpt-5.6-sol",
        "issuer": "issuer",
        "checkpoint": {
            "type": "compaction",
            "encrypted_content": "opaque",
        },
        # No tool-loop messages preceded the response, so the current user is
        # the last input represented by this checkpoint.
        "_covered_turn_message_count": 0,
    }

    AgentLoop._commit_provider_continuity(
        session,
        state,
        turn_start_index=turn_start,
    )

    stored = session.metadata["provider_continuity"]
    assert stored["covered_event_id"] == session.messages[turn_start][EVENT_ID_KEY]
    assert stored["covered_event_id"] != session.messages[-1][EVENT_ID_KEY]
    assert "_covered_turn_message_count" not in stored


def test_reset_drops_encrypted_conversation_state() -> None:
    session = Session(key="web:one")
    session.metadata["provider_continuity"] = {
        "checkpoint": {"type": "compaction", "encrypted_content": "opaque"}
    }

    session.reset_conversation_context()

    assert "provider_continuity" not in session.metadata
