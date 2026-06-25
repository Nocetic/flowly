"""Agency-first system prompt: strong identity + positive principles for all
models, strict prohibition-style discipline gated to the models that need it.

Mirrors the upstream structure: capable models (Claude, Grok) run on the
agentic base and skip the explicit tool-use enforcement hammer; weaker and
unknown/local models still get it. The point is that the flagship models stop
feeling boxed-in by weak-model steering they don't need.
"""

from flowly.agent.prompt_blocks import (
    build_agency_block,
    build_discipline_block,
    model_needs_strict_discipline,
)


def test_strong_models_skip_strict_discipline():
    # Claude and Grok/xAI are action-oriented; they get the agentic base only.
    for m in [
        "anthropic/claude-opus-4.8",
        "claude-3-5-sonnet",
        "x-ai/grok-4",
        "grok-beta",
    ]:
        assert model_needs_strict_discipline(m) is False, m


def test_weak_and_unknown_models_get_strict_discipline():
    # Weak families AND unknown/local/None default to strict (conservative):
    # better to over-steer an unknown model than under-steer it.
    for m in [
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
        "qwen/qwen-3-max",
        "deepseek/deepseek-v3",
        "meta-llama/llama-3.3-70b",
        "some-local-model",
        None,
        "",
    ]:
        assert model_needs_strict_discipline(m) is True, m


def test_agency_block_is_positive_and_substantive():
    b = build_agency_block()
    assert b and len(b) > 200
    low = b.lower()
    # Positive, principle-framed signals — the agentic voice.
    for kw in ["finish", "judgment", "honest", "tool"]:
        assert kw in low, kw
    # It is NOT a prohibition wall: far fewer "never/do not" than the strict block.
    negatives = low.count("never") + low.count("do not") + low.count("don't")
    assert negatives <= 3, f"agency block reads prohibition-heavy ({negatives})"


def test_discipline_block_still_strict():
    # The gated block keeps its explicit enforcement (for weak models).
    d = build_discipline_block()
    assert "<mandatory_tool_use>" in d
    assert "<act_dont_ask>" in d
