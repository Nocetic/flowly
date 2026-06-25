"""Tests for Session tool-protocol persistence.

These pin the contract that lets multi-turn conversations carry tool
reasoning across turns:

  * assistant.tool_calls and tool.tool_call_id / name flow through
    get_history unchanged — without them the LLM can't link a tool
    result to its triggering call and providers reject the request.
  * Orphan tool_calls (assistant_with_tool_calls without matching
    tool replies, or tool messages whose triggering assistant is
    missing ids) get trimmed from the tail so a crashed-mid-turn
    session resumes cleanly instead of throwing a provider 400.

This is the ChatGPT-style full-structure persistence pattern, ported
into Flowly's previously text-only session store.
"""

from __future__ import annotations

from flowly.session.manager import (
    Session,
    _project_for_llm,
    _repair_tool_sequence,
)


# ---------------------------------------------------------------------------
# get_history field preservation
# ---------------------------------------------------------------------------


class TestGetHistoryFieldPreservation:
    def test_plain_assistant_unchanged(self) -> None:
        s = Session(key="t1")
        s.add_message("user", "hi")
        s.add_message("assistant", "hello")
        assert s.get_history() == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_assistant_with_tool_calls_preserves_them(self) -> None:
        """Assistant message issuing a tool call must keep the
        tool_calls list intact — the LLM uses these ids to link the
        following tool result."""
        s = Session(key="t1")
        tool_calls = [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "exec", "arguments": '{"command":"ls"}'},
        }]
        s.add_message(
            "assistant",
            "Let me check.",
            tool_calls=tool_calls,
        )
        s.add_message(
            "tool",
            "file1\nfile2",
            tool_call_id="call_abc",
            name="exec",
        )
        s.add_message("assistant", "Found two files.")

        h = s.get_history()
        # Assistant carries tool_calls
        assert h[0]["tool_calls"] == tool_calls
        # Tool result carries tool_call_id + name (required by OpenAI / Anthropic)
        assert h[1]["tool_call_id"] == "call_abc"
        assert h[1]["name"] == "exec"
        # Plain assistant has neither
        assert "tool_calls" not in h[2]
        assert "tool_call_id" not in h[2]

    def test_internal_fields_stripped(self) -> None:
        """timestamp + ad-hoc bookkeeping fields must NOT reach the LLM —
        OpenAI rejects unknown top-level fields on chat-completions
        messages with a 400."""
        s = Session(key="t1")
        s.add_message("user", "hi", custom_audit_id="a-123")
        h = s.get_history()
        assert "timestamp" not in h[0]
        assert "custom_audit_id" not in h[0]

    def test_multimodal_tool_content_passes_through(self) -> None:
        """Vision-capable tool results land as list[dict] (text +
        image_url blocks). This shape must survive the LLM
        projection so screenshot tool calls keep their visual
        content. Without this, browser_tab screenshots lose the
        image and the model just sees a sanitized URL string."""
        s = Session(key="t1")
        tool_calls = [{"id": "c1", "type": "function", "function": {"name": "browser_tab", "arguments": "{}"}}]
        s.add_message("assistant", "looking", tool_calls=tool_calls)
        content_blocks = [
            {"type": "text", "text": "Page captured."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]
        s.add_message("tool", content_blocks, tool_call_id="c1", name="browser_tab")
        h = s.get_history()
        assert h[1]["content"] == content_blocks


# ---------------------------------------------------------------------------
# Orphan tool-call repair
# ---------------------------------------------------------------------------


class TestRepairToolSequence:
    def test_complete_sequence_untouched(self) -> None:
        """A clean assistant_with_tool_calls + matching tool + final
        assistant sequence must pass through unmodified — the repair
        function only trims malformed tails."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "ok", "tool_call_id": "x", "name": "exec"},
            {"role": "assistant", "content": "done"},
        ]
        assert _repair_tool_sequence(messages) == messages

    def test_trailing_orphan_assistant_with_tool_calls_dropped(self) -> None:
        """Crash scenario: agent emitted tool_calls but the subprocess
        died before any tool result was written. Resuming would send
        the LLM an assistant_with_tool_calls that has no follow-up,
        which providers 400 on. Drop the orphan so resume is clean."""
        orphan = {
            "role": "assistant",
            "content": "Let me run this",
            "tool_calls": [{"id": "y", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        }
        messages = [
            {"role": "user", "content": "test"},
            orphan,
        ]
        repaired = _repair_tool_sequence(messages)
        assert orphan not in repaired
        assert repaired == [{"role": "user", "content": "test"}]

    def test_trailing_tool_with_missing_partner_drops_pair(self) -> None:
        """Crash scenario: assistant issued TWO tool calls (id=a, id=b),
        only id=a got a result before the crash. Resuming with
        ``assistant_with_tool_calls(a,b) + tool(a)`` triggers a 400
        because tool ``b`` is unsatisfied. Drop both the partial-
        results tail AND the issuing assistant — the next user turn
        will land on a clean boundary."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "running", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "a-result", "tool_call_id": "a", "name": "exec"},
        ]
        repaired = _repair_tool_sequence(messages)
        assert repaired == [{"role": "user", "content": "test"}]

    def test_all_ids_satisfied_keeps_sequence(self) -> None:
        """Both tool calls answered → sequence stays."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "a", "tool_call_id": "a", "name": "exec"},
            {"role": "tool", "content": "b", "tool_call_id": "b", "name": "exec"},
        ]
        assert _repair_tool_sequence(messages) == messages

    def test_tool_messages_without_any_preceding_assistant_dropped(self) -> None:
        """Pathological state: tool messages but the assistant before
        them isn't there (e.g. compaction kept the tools but dropped
        their issuer). Drop the orphan tools."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "tool", "content": "huh?", "tool_call_id": "x", "name": "exec"},
        ]
        repaired = _repair_tool_sequence(messages)
        assert repaired == [{"role": "user", "content": "test"}]

    def test_does_not_mutate_input(self) -> None:
        """Caller may rely on the original list being unchanged."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "x", "tool_calls": [
                {"id": "y", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ]},
        ]
        snapshot = [dict(m) for m in messages]
        _repair_tool_sequence(messages)
        assert messages == snapshot

    def test_get_history_invokes_repair(self) -> None:
        """End-to-end: a session that ends on an orphan
        assistant_with_tool_calls must come out of get_history
        without it."""
        s = Session(key="t1")
        s.add_message("user", "test")
        s.add_message(
            "assistant",
            "running",
            tool_calls=[{"id": "y", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        )
        h = s.get_history()
        # Orphan stripped, only the user message remains.
        assert h == [{"role": "user", "content": "test"}]


# ---------------------------------------------------------------------------
# _project_for_llm direct
# ---------------------------------------------------------------------------


class TestProjectForLlm:
    def test_drops_unknown_fields(self) -> None:
        msg = {
            "role": "assistant",
            "content": "hi",
            "timestamp": "2026-01-01",
            "private_audit_flag": True,
        }
        assert _project_for_llm(msg) == {"role": "assistant", "content": "hi"}

    def test_keeps_tool_calls_for_assistant_only(self) -> None:
        """tool_calls field is only meaningful on the assistant role.
        If somehow it appeared on a user message it would be a bug —
        the projection drops it rather than confusing the provider."""
        # Assistant: keeps it
        msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}
        out = _project_for_llm(msg)
        assert out.get("tool_calls") == [{"id": "x"}]

        # User (bogus state): drops it
        msg2 = {"role": "user", "content": "hi", "tool_calls": [{"id": "x"}]}
        out2 = _project_for_llm(msg2)
        assert "tool_calls" not in out2

    def test_keeps_tool_call_id_and_name_for_tool_only(self) -> None:
        msg = {
            "role": "tool",
            "content": "result",
            "tool_call_id": "abc",
            "name": "exec",
        }
        out = _project_for_llm(msg)
        assert out == {
            "role": "tool",
            "content": "result",
            "tool_call_id": "abc",
            "name": "exec",
        }


# ---------------------------------------------------------------------------
# extend_with_turn_messages — the helper loop.py uses to persist turns
# ---------------------------------------------------------------------------


def _tc(call_id: str, name: str = "exec", args: str = "{}") -> dict:
    """Build a synthetic tool_call dict matching OpenAI/Anthropic shape."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class TestExtendWithTurnMessages:
    """Pins the persistence recipe loop.py uses at the end of every turn.

    The helper takes the user prompt + the slice of loop messages
    that were appended during this turn + the post-processed
    final_content, and writes them all to the session. Tests assert
    that the saved transcript ends LLM-protocol-valid AND matches
    what the user actually saw streaming.
    """

    def test_plain_text_turn_no_tools(self) -> None:
        """Simplest path: model produced one assistant text response,
        no tool calls. Saved as just user + assistant."""
        s = Session(key="t")
        # Simulate what _run_llm_tool_loop's local messages would
        # contain after a tool-free turn: just the assistant reply.
        new_msgs = [{"role": "assistant", "content": "Hello!"}]
        s.extend_with_turn_messages(
            user_content="hi",
            new_messages=new_msgs,
            final_content="Hello!",
        )
        roles = [m["role"] for m in s.messages]
        assert roles == ["user", "assistant"]
        assert s.messages[-1]["content"] == "Hello!"

    def test_tool_turn_preserves_full_structure(self) -> None:
        """A single-tool turn must save the full
        assistant_with_tool_calls + tool_result + final_assistant
        sequence so next turn's LLM sees the tool reasoning."""
        s = Session(key="t")
        new_msgs = [
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [_tc("c1", "exec", '{"command":"ls"}')],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "exec",
                "content": "file1\nfile2",
            },
            {"role": "assistant", "content": "Found two files."},
        ]
        s.extend_with_turn_messages(
            user_content="what's in here?",
            new_messages=new_msgs,
            final_content="Found two files.",
        )
        roles = [m["role"] for m in s.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]

        # Tool-protocol fields preserved on the saved messages.
        assert s.messages[1]["tool_calls"] == [_tc("c1", "exec", '{"command":"ls"}')]
        assert s.messages[2]["tool_call_id"] == "c1"
        assert s.messages[2]["name"] == "exec"

        # get_history returns the full structure ready for the LLM.
        h = s.get_history()
        assert h[1]["tool_calls"] == [_tc("c1", "exec", '{"command":"ls"}')]
        assert h[2]["tool_call_id"] == "c1"

    def test_final_content_overrides_closing_assistant_text(self) -> None:
        """When voice-mode sanitization (or other post-processing)
        changes the closing assistant text, the persisted version
        must reflect what the USER saw — not the raw loop output.
        Otherwise voice users get TTS-friendly text in real time but
        markdown-laden text in chat history. Inconsistent UX."""
        s = Session(key="t")
        new_msgs = [
            {"role": "assistant", "content": "**Hello!** _Bold_ http://example.com"},
        ]
        s.extend_with_turn_messages(
            user_content="hi",
            new_messages=new_msgs,
            final_content="Hello! Bold",  # post-sanitization
        )
        # Saved message uses the sanitized version.
        assert s.messages[-1]["content"] == "Hello! Bold"

    def test_tool_call_assistant_preamble_NOT_overridden(self) -> None:
        """Only the CLOSING plain-text assistant gets final_content.
        Tool-call assistant messages have their own preamble that
        belongs alongside the tool calls — clobbering it would lose
        the model's reasoning ('Let me check this first…'). The
        closing assistant is the one whose content matches what the
        user saw as the final reply."""
        s = Session(key="t")
        new_msgs = [
            {
                "role": "assistant",
                "content": "I'll search first.",
                "tool_calls": [_tc("c1", "search", "{}")],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "search", "content": "results"},
            {"role": "assistant", "content": "Here's the answer."},
        ]
        s.extend_with_turn_messages(
            user_content="q",
            new_messages=new_msgs,
            final_content="Here's the answer.",
        )
        # Tool-call assistant's preamble preserved.
        assert s.messages[1]["content"] == "I'll search first."
        # Closing assistant uses final_content (matches by luck here).
        assert s.messages[3]["content"] == "Here's the answer."

    def test_loop_ended_on_tool_calls_appends_capstone(self) -> None:
        """Pathological turn: the loop hit max iterations or errored
        out with an open tool_calls and no closing assistant text.
        ``final_content`` carries a synthesised fallback ("Action
        executed."). We append it as a capstone so the saved
        transcript ends on a plain-text assistant the next turn can
        extend from. Without the capstone, repair would trim the
        trailing tool / assistant_with_tool_calls on next load
        (orphan), losing this turn's record entirely."""
        s = Session(key="t")
        new_msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tc("c1", "exec", "{}")],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "stdout"},
            # Note: NO closing plain-text assistant here — loop ended early.
        ]
        s.extend_with_turn_messages(
            user_content="do it",
            new_messages=new_msgs,
            final_content="Action executed.",  # synthesised fallback
        )
        roles = [m["role"] for m in s.messages]
        # user + assistant_with_tool_calls + tool + capstone assistant
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert s.messages[-1]["content"] == "Action executed."
        assert "tool_calls" not in s.messages[-1]

        # Critically, get_history's orphan repair leaves this intact —
        # the capstone makes the sequence valid.
        h = s.get_history()
        assert len(h) == 4
        assert h[-1] == {"role": "assistant", "content": "Action executed."}

    def test_multi_iteration_turn_persists_all(self) -> None:
        """A turn with two tool iterations — assistant→tool→assistant→tool→
        closing — must save EVERY message. Without this, an LLM on
        next turn can't see that we already searched for X, ran exec
        Y, etc. The whole point of full-structure persistence."""
        s = Session(key="t")
        new_msgs = [
            {"role": "assistant", "content": "Step 1", "tool_calls": [_tc("a")]},
            {"role": "tool", "tool_call_id": "a", "name": "exec", "content": "ok1"},
            {"role": "assistant", "content": "Step 2", "tool_calls": [_tc("b")]},
            {"role": "tool", "tool_call_id": "b", "name": "exec", "content": "ok2"},
            {"role": "assistant", "content": "Done."},
        ]
        s.extend_with_turn_messages(
            user_content="multi-step task",
            new_messages=new_msgs,
            final_content="Done.",
        )
        roles = [m["role"] for m in s.messages]
        assert roles == [
            "user",
            "assistant", "tool",
            "assistant", "tool",
            "assistant",
        ]

    def test_next_turn_sees_prior_tool_history(self) -> None:
        """The whole point of this work: a tool turn in conversation
        step 1 must be visible to the LLM in conversation step 2.
        Simulate two consecutive turns and assert the second
        ``get_history`` includes the first turn's tool sequence."""
        s = Session(key="t")

        # Turn 1: user asks, agent runs a tool, replies.
        s.extend_with_turn_messages(
            user_content="search for foo",
            new_messages=[
                {"role": "assistant", "content": "", "tool_calls": [_tc("s1", "search")]},
                {"role": "tool", "tool_call_id": "s1", "name": "search", "content": "found foo"},
                {"role": "assistant", "content": "I found foo."},
            ],
            final_content="I found foo.",
        )

        # Turn 2: history fed into LLM must include the search.
        history = s.get_history()
        assert any(
            m.get("role") == "assistant" and m.get("tool_calls")
            for m in history
        ), "LLM history must surface the tool_call from turn 1"
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "s1"
            for m in history
        ), "LLM history must surface the tool result from turn 1"
