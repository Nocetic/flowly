"""Subagents must survive a long run instead of dying on the provider's limit.

A subagent's context was managed purely by iteration count: truncate old tool
results after 3 iterations, harder after 7. Iteration count is a poor proxy
for context pressure — three iterations that each read a large file overflow
long before the fixed thresholds fire, and a 32K model overflows sooner than
a 200K one regardless of how many iterations have run.

Subagents deliberately do NOT summarize. Their context is disposable by
design, so a summarization call inside one doubles its cost for work that is
discarded when it returns. Progressive truncation is the cheap half.
"""

import pytest

from flowly.agent.subagent import (
    _SUBAGENT_RESERVE_TOKENS,
    _subagent_context_budget,
    _trim_to_context_budget,
)
from flowly.compaction.estimator import estimate_messages_tokens


# Realistic prose, not a repeated character: a long run of one character is a
# pathological input for BPE tokenizers and made this file take minutes.
_LINE = "2026-08-04 04:12:33 INFO worker finished batch 41 in 182ms; queue depth 7\n"


def _payload(chars: int) -> str:
    return (_LINE * (chars // len(_LINE) + 1))[:chars]


def _tool_heavy_history(turns: int, payload_chars: int = 2_000) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "You are a subagent."}]
    for i in range(turns):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"c{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "big.log"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "name": "read_file",
            "content": f"batch {i}\n" + _payload(payload_chars),
        })
    return messages


# ── Budget follows the model ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "model,expected_window",
    [
        ("anthropic/claude-haiku-4.5", 200_000),
        ("openai/gpt-3.5-turbo", 16_385),
        ("google/gemini-2.5-pro", 1_000_000),
    ],
)
def test_budget_is_derived_from_the_models_real_window(model, expected_window):
    assert _subagent_context_budget(model) == expected_window - _SUBAGENT_RESERVE_TOKENS


def test_unknown_model_still_gets_a_usable_budget():
    budget = _subagent_context_budget("some/unreleased-model")

    assert budget > 0


def test_budget_never_collapses_to_nothing_on_a_tiny_window():
    # Reserve larger than the window must not yield a negative budget.
    assert _subagent_context_budget("openai/gpt-3.5-turbo") >= 8_000


# ── Trimming ──────────────────────────────────────────────────────────────


def test_history_within_budget_is_left_alone():
    messages = _tool_heavy_history(2, payload_chars=100)

    assert _trim_to_context_budget(messages, "anthropic/claude-haiku-4.5") is messages


def test_oversized_history_is_brought_under_the_budget():
    # 30 large tool results blow past a 16K window many times over.
    messages = _tool_heavy_history(30)
    model = "openai/gpt-3.5-turbo"
    before = estimate_messages_tokens(messages)

    trimmed = _trim_to_context_budget(messages, model)

    assert before > _subagent_context_budget(model), "fixture must actually overflow"
    assert estimate_messages_tokens(trimmed) <= _subagent_context_budget(model)


def test_trimming_keeps_the_conversation_usable():
    messages = _tool_heavy_history(30)

    trimmed = _trim_to_context_budget(messages, "openai/gpt-3.5-turbo")

    assert trimmed[0]["role"] == "system", "the instructions must survive"
    assert len(trimmed) == len(messages), "turns are truncated, never dropped"
    assert trimmed[-1]["content"].startswith("batch "), "the newest result stays readable"


def test_a_small_window_trims_where_a_large_one_would_not():
    """The whole point: the same history is fine on one model and not another."""
    messages = _tool_heavy_history(20)

    on_small = _trim_to_context_budget(list(messages), "openai/gpt-3.5-turbo")
    on_large = _trim_to_context_budget(list(messages), "google/gemini-2.5-pro")

    assert estimate_messages_tokens(on_small) < estimate_messages_tokens(on_large)


def test_trimming_is_stable_when_run_twice():
    messages = _tool_heavy_history(30)
    model = "openai/gpt-3.5-turbo"

    once = _trim_to_context_budget(messages, model)
    twice = _trim_to_context_budget(once, model)

    assert estimate_messages_tokens(twice) <= estimate_messages_tokens(once)


def test_pathological_history_returns_something_rather_than_looping():
    """A single message larger than the whole window cannot be truncated away.
    Returning it (and logging) beats spinning or raising."""
    messages = [{"role": "user", "content": _payload(80_000)}]

    trimmed = _trim_to_context_budget(messages, "openai/gpt-3.5-turbo")

    assert trimmed == messages
