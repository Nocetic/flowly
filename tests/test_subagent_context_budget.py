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
    # It used to be exactly ``window - reserve``. That formula budgeted the
    # message list as though it were the whole request, which is how a
    # 16K model ended up planning a 25K one. The budget still scales with the
    # window — it just now shares it with the reply and the overhead.
    budget = _subagent_context_budget(model)

    assert 0 < budget < expected_window
    assert budget > expected_window * 0.3, "a subagent must still get room to think"


def test_unknown_model_still_gets_a_usable_budget():
    budget = _subagent_context_budget("some/unreleased-model")

    assert budget > 0


def test_budget_never_collapses_to_nothing_on_a_tiny_window():
    # A reserve larger than the window must not yield a negative budget — and
    # must not eat the whole window either, which is the failure the share cap
    # exists for.
    budget = _subagent_context_budget("openai/gpt-3.5-turbo")

    assert budget >= 1_000
    assert budget < 16_385


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


# ── One window, three claimants ───────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    ["openai/gpt-3.5-turbo", "openai/gpt-4o", "anthropic/claude-haiku-4.5",
     "google/gemini-2.5-pro", "unknown/model"],
)
def test_the_whole_request_fits_the_model(model):
    """The message list, the reply and the fixed overhead were each sized as
    if they were alone. On a 16,385-token model that meant 8,385 tokens of
    messages plus a 16,384-token reply plus tool schemas — 25K into a 16K
    window, rejected every time, and no amount of trimming the messages could
    help because the overflow was the reply budget itself."""
    from flowly.agent.subagent import (
        _SUBAGENT_OVERHEAD_TOKENS,
        _subagent_context_budget,
        subagent_context_window,
        subagent_output_budget,
    )

    window = subagent_context_window(model)
    planned = (
        _subagent_context_budget(model)
        + subagent_output_budget(model)
        + _SUBAGENT_OVERHEAD_TOKENS
    )

    assert planned < window, (
        f"{model}: plans a {planned}-token request for a {window}-token window"
    )


@pytest.mark.parametrize("model", ["openai/gpt-3.5-turbo", "anthropic/claude-haiku-4.5"])
def test_a_subagent_always_gets_room_to_think(model):
    """The clamps must not solve the overflow by leaving nothing to read."""
    from flowly.agent.subagent import _subagent_context_budget, subagent_output_budget

    assert _subagent_context_budget(model) >= 1_000
    assert subagent_output_budget(model) >= 512


def test_a_large_model_keeps_the_full_reply_budget():
    """The clamp is a ceiling for small windows, not a downgrade for everyone."""
    from flowly.agent.subagent import _SUBAGENT_MAX_OUTPUT_TOKENS, subagent_output_budget

    assert subagent_output_budget("anthropic/claude-haiku-4.5") == _SUBAGENT_MAX_OUTPUT_TOKENS
