"""Encrypted provider checkpoint placement, accounting, and persistence."""

from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.queue import MessageBus
from flowly.compaction.estimator import estimate_messages_tokens
from flowly.config.schema import Config
from flowly.providers.base import (
    PROVIDER_COMPACTION_CHECKPOINT_KEY,
    PROVIDER_REPLAY_KEY,
    LLMProvider,
    LLMResponse,
)
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


def test_post_checkpoint_reasoning_replay_is_budgeted_but_prior_replay_is_not() -> None:
    def replay_message(blob: str) -> dict:
        return {
            "role": "assistant",
            "content": "answer",
            PROVIDER_REPLAY_KEY: {
                "items": [{
                    "type": "reasoning",
                    "encrypted_content": blob,
                }],
            },
        }

    prior = replay_message("a" * 20_000)
    tail = replay_message("b" * 20_000)
    baseline = estimate_messages_tokens([_marker(), tail])
    total = estimate_messages_tokens([prior, _marker(), tail])

    assert total == baseline
    assert baseline > estimate_messages_tokens([_marker(), {
        "role": "assistant",
        "content": "answer",
    }])


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


@pytest.mark.asyncio
async def test_final_provider_replay_is_journaled_and_persisted(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    replay = {
        "provider": "openai_codex",
        "model": "gpt-5.6-sol",
        "issuer": "issuer",
        "continuity_id": "epoch",
        "items": [{
            "type": "reasoning",
            "encrypted_content": "opaque-reasoning",
            "summary": [],
        }],
    }

    class Provider(LLMProvider):
        def get_default_model(self) -> str:
            return "gpt-5.6-sol"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(
                content="answer",
                provider_replay=replay,
            )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(api_key="test"),
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=2,
        soft_warn_at_iteration=0,
    )
    turn_messages: list[dict] = []
    try:
        final, *_ = await loop._run_llm_tool_loop(
            messages=[
                {"role": "system", "content": "test"},
                {"role": "user", "content": "question"},
            ],
            action_turn=False,
            turn_content="question",
            session_key="web:one",
            turn_messages_out=turn_messages,
        )
    finally:
        loop.stop()

    assert final == "answer"
    assert turn_messages == [{
        "role": "assistant",
        "content": "answer",
        PROVIDER_REPLAY_KEY: replay,
    }]
    session = Session(key="web:one")
    session.extend_with_turn_messages(
        user_content="question",
        new_messages=turn_messages,
        final_content=final,
    )
    assert session.messages[-1][PROVIDER_REPLAY_KEY] == replay
