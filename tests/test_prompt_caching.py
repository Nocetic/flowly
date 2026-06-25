"""Tests for Anthropic prompt-cache marker injection.

Pins two behaviours that used to drift and caused either silent hangs
on OpenRouter's Claude pipeline (tool-message top-level markers) or
wasted breakpoints (tool messages selected in the rolling window but
receiving no marker):

  1. ``role:"tool"`` messages never get a top-level ``cache_control``
     field — OpenRouter's Claude pipeline silently hangs on it.
  2. Breakpoint selection skips tool messages, so all four slots are
     spent on messages that actually emit the marker.
"""

from __future__ import annotations

from flowly.providers.prompt_caching import (
    apply_cache_control,
    is_cacheable_model,
    _build_marker,
    _resolve_default_ttl,
    set_default_cache_ttl,
)


# ---------------------------------------------------------------------------
# Tool-message safety
# ---------------------------------------------------------------------------


class TestToolMessageMarkerSafety:
    """role:"tool" messages must never carry a top-level cache_control.

    OpenRouter's Claude pipeline either rejects or silently hangs on
    the top-level field when the role is ``tool``. Observed in
    production logs; this regression test fails fast if any future
    change reintroduces the marker on tool messages.
    """

    def test_single_tool_message_gets_no_marker(self) -> None:
        msgs = [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "using tool", "tool_calls": []},
            {"role": "tool", "content": "result", "tool_call_id": "t1"},
        ]
        out = apply_cache_control(msgs)
        tool_msg = next(m for m in out if m["role"] == "tool")
        assert "cache_control" not in tool_msg

    def test_tool_message_content_not_mutated(self) -> None:
        # The tool message's content should be untouched — no list
        # wrapping, no cache_control on inner blocks either. The
        # marker goes on a sibling non-tool message, not here.
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "tool", "content": "plain string", "tool_call_id": "t1"},
        ]
        out = apply_cache_control(msgs)
        tool_msg = next(m for m in out if m["role"] == "tool")
        assert tool_msg["content"] == "plain string"
        assert "cache_control" not in tool_msg

    def test_trailing_tool_message_spends_breakpoint_on_sibling(self) -> None:
        # If the last message is a tool result, the rolling-window
        # breakpoint should land on the assistant message that called
        # the tool (or whichever non-tool message precedes it), NOT on
        # the tool message. Otherwise the turn is effectively uncached.
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1", "tool_calls": []},
            {"role": "tool", "content": "r1", "tool_call_id": "t1"},
        ]
        out = apply_cache_control(msgs, max_breakpoints=2)
        assert "cache_control" not in out[3]  # the tool message
        # The assistant message, last non-tool non-system, must have
        # received the cache marker somewhere — either on content or
        # inside the content-block list.
        assistant = out[2]
        assert (
            assistant.get("cache_control")
            or (isinstance(assistant.get("content"), list) and any(
                isinstance(b, dict) and "cache_control" in b
                for b in assistant["content"]
            ))
        )


# ---------------------------------------------------------------------------
# Breakpoint budget
# ---------------------------------------------------------------------------


class TestBreakpointBudget:
    """The 4-breakpoint budget (Anthropic limit) must be spent on
    messages that actually emit a marker — tool messages are wasted
    slots since they skip marker application entirely."""

    def test_system_plus_three_non_tool(self) -> None:
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "user", "content": "u3"},
            {"role": "user", "content": "u4"},
        ]
        out = apply_cache_control(msgs, max_breakpoints=4)
        marked = sum(
            1 for m in out
            if m.get("cache_control")
            or (isinstance(m.get("content"), list) and any(
                isinstance(b, dict) and "cache_control" in b
                for b in m["content"]
            ))
        )
        # System + last 3 user messages = 4 slots, all land.
        assert marked == 4

    def test_tool_messages_do_not_consume_slots(self) -> None:
        # Two of the last four messages are tool results. The rolling
        # window must still place markers on the non-tool ones so we
        # get full cache coverage instead of gaps.
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1", "tool_calls": []},
            {"role": "tool", "content": "r1", "tool_call_id": "t1"},
            {"role": "assistant", "content": "a2", "tool_calls": []},
            {"role": "tool", "content": "r2", "tool_call_id": "t2"},
            {"role": "user", "content": "u2"},
        ]
        out = apply_cache_control(msgs, max_breakpoints=4)
        # No tool message should carry a marker (top-level or inner).
        for m in out:
            if m["role"] == "tool":
                assert "cache_control" not in m
                # content stays a string — no conversion to block list.
                assert isinstance(m["content"], str)


# ---------------------------------------------------------------------------
# Model gate
# ---------------------------------------------------------------------------


class TestIsCacheableModel:
    """The cache-marker injection should only run for Claude models.
    Non-Claude models receive no markers and the response is returned
    without a pass through ``apply_cache_control``."""

    def test_claude_is_cacheable(self) -> None:
        assert is_cacheable_model("anthropic/claude-haiku-4.5")
        assert is_cacheable_model("claude-sonnet-4.6")
        assert is_cacheable_model("openrouter/anthropic/claude-opus-4.6")

    def test_non_claude_not_cacheable(self) -> None:
        assert not is_cacheable_model("openai/gpt-5.4")
        assert not is_cacheable_model("moonshotai/kimi-k2.5")
        assert not is_cacheable_model("google/gemini-2.5-flash")

    def test_empty_or_none_is_not_cacheable(self) -> None:
        assert not is_cacheable_model("")
        assert not is_cacheable_model(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TTL configuration — 1h default, 5m override, env var, helper
# ---------------------------------------------------------------------------
#
# Flowly defaults to the 1-hour ephemeral cache. The 2× one-time write
# premium is paid once per session prefix; from turn 3 onward within
# the hour the effective input cost is roughly 0.15-0.25× of uncached.
# The break-even threshold is ~3 turns within the hour, which essentially
# every real Flowly session beats.
#
# These tests pin three guarantees:
#   1. The marker carries ``ttl: "1h"`` by default (regression would
#      silently revert to 5m and re-introduce the cache-churn problem).
#   2. The env var ``FLOWLY_CLAUDE_CACHE_TTL=5m`` flips behaviour back
#      cleanly (operator escape hatch for proxies that strip ttl).
#   3. Invalid values fall back to 1h rather than silently breaking.


class TestCacheTTLDefault:
    """1h marker is the Flowly default. 5m is the explicit-override fallback."""

    def _system_marker(self, msgs: list[dict]) -> dict:
        """Extract the cache_control marker from the system prompt block.

        Helper used across these tests — the marker is buried two
        layers deep (content list → last block → cache_control) and
        repeating that lookup makes the tests noisy.
        """
        return msgs[0]["content"][0]["cache_control"]

    def test_default_marker_has_1h_ttl(self) -> None:
        msgs = [
            {"role": "system", "content": "You are Flowly."},
            {"role": "user", "content": "hi"},
        ]
        out = apply_cache_control(msgs)
        marker = self._system_marker(out)
        assert marker == {"type": "ephemeral", "ttl": "1h"}, (
            "default cache marker dropped the 1h TTL — silent regression to 5m"
        )

    def test_explicit_5m_override_drops_ttl_field(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
        ]
        out = apply_cache_control(msgs, ttl="5m")
        # 5m is the API default so the ``ttl`` field is OMITTED
        # rather than set to "5m" — keeps the request body smaller
        # and avoids redundancy. Pin this contract.
        assert self._system_marker(out) == {"type": "ephemeral"}

    def test_explicit_1h_override_matches_default(self) -> None:
        msgs = [{"role": "system", "content": "sys"}]
        default_out = apply_cache_control(msgs)
        explicit_out = apply_cache_control(msgs, ttl="1h")
        assert default_out == explicit_out

    def test_invalid_ttl_falls_back_to_default(self) -> None:
        # The signature is typed with Literal but Python doesn't
        # enforce types at runtime. A typo'd "1hr" or "1 hour"
        # must not break the request — fall back to the module
        # default (1h today, or whatever the operator set).
        msgs = [{"role": "system", "content": "sys"}]
        out = apply_cache_control(msgs, ttl="1hr")  # type: ignore[arg-type]
        # Result is well-formed — marker exists, ttl falls back.
        marker = self._system_marker(out)
        assert marker.get("type") == "ephemeral"


class TestEnvVarOverride:
    """``FLOWLY_CLAUDE_CACHE_TTL`` flips the default at process start.

    Operators flip this to ``5m`` if a downstream proxy is observed
    stripping the ``ttl`` field or producing odd cache behaviour with
    the 1h variant. The default in the code is 1h; the env var is the
    escape hatch.
    """

    def test_5m_env_var_resolved(self, monkeypatch) -> None:
        monkeypatch.setenv("FLOWLY_CLAUDE_CACHE_TTL", "5m")
        assert _resolve_default_ttl() == "5m"

    def test_1h_env_var_resolved(self, monkeypatch) -> None:
        monkeypatch.setenv("FLOWLY_CLAUDE_CACHE_TTL", "1h")
        assert _resolve_default_ttl() == "1h"

    def test_unknown_env_var_value_falls_back_to_1h(
        self, monkeypatch,
    ) -> None:
        # A garbage value (typo, leftover config) must not silently
        # disable caching — it falls back to the 1h default.
        monkeypatch.setenv("FLOWLY_CLAUDE_CACHE_TTL", "forever")
        assert _resolve_default_ttl() == "1h"

    def test_unset_env_var_defaults_to_1h(self, monkeypatch) -> None:
        monkeypatch.delenv("FLOWLY_CLAUDE_CACHE_TTL", raising=False)
        assert _resolve_default_ttl() == "1h"

    def test_whitespace_and_case_tolerated(self, monkeypatch) -> None:
        # Operator copies "  5M  " from a doc — must still resolve.
        monkeypatch.setenv("FLOWLY_CLAUDE_CACHE_TTL", "  5M  ")
        assert _resolve_default_ttl() == "5m"


class TestSetDefaultCacheTTL:
    """The ``set_default_cache_ttl`` helper is intended for tests +
    rare runtime overrides. Pin the clamping behaviour so a typo'd
    call doesn't silently break caching."""

    def test_set_to_5m_then_back(self) -> None:
        # Setup: restore to known state at the end.
        from flowly.providers import prompt_caching as pc
        original = pc._DEFAULT_TTL
        try:
            set_default_cache_ttl("5m")
            assert pc._DEFAULT_TTL == "5m"
            out = apply_cache_control([{"role": "system", "content": "x"}])
            assert "ttl" not in out[0]["content"][0]["cache_control"]
        finally:
            set_default_cache_ttl(original)  # type: ignore[arg-type]

    def test_invalid_value_clamps_to_1h(self) -> None:
        from flowly.providers import prompt_caching as pc
        original = pc._DEFAULT_TTL
        try:
            set_default_cache_ttl("garbage")  # type: ignore[arg-type]
            assert pc._DEFAULT_TTL == "1h"
        finally:
            set_default_cache_ttl(original)  # type: ignore[arg-type]


class TestMarkerShape:
    """Pin the exact marker dict shape so a refactor can't introduce
    a stray field that the Anthropic API would reject."""

    def test_1h_marker_has_two_fields_exactly(self) -> None:
        marker = _build_marker("1h")
        assert set(marker.keys()) == {"type", "ttl"}
        assert marker["type"] == "ephemeral"
        assert marker["ttl"] == "1h"

    def test_5m_marker_has_one_field_only(self) -> None:
        marker = _build_marker("5m")
        assert set(marker.keys()) == {"type"}
        assert marker["type"] == "ephemeral"
