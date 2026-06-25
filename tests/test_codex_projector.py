"""Tests for the Codex event projector.

The projector translates Codex's item-stream notification dicts
into Flowly's flat message format. These tests pin the contract for
each item type by feeding in a captured-style notification sequence
and asserting on the produced messages, final text, reasoning
items, and tool-iteration count.

We don't depend on the JSON-RPC transport here — the projector is
pure dict-in / dataclass-out. That keeps these tests fast
(<1s for the whole class) and lets us cover protocol corner cases
that would be awkward to reproduce with a real subprocess.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flowly.codex.projector import (
    CodexEventProjector,
    TurnProjection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _feed(projector: CodexEventProjector, notes: list[dict[str, Any]]) -> None:
    """Feed a list of notification dicts to the projector in order."""
    for note in notes:
        await projector.handle_notification(note)


def _started(item_id: str, item_type: str, **payload: Any) -> dict[str, Any]:
    """Build a synthetic ``item/started`` notification."""
    return {
        "method": "item/started",
        "params": {"itemId": item_id, "type": item_type, **payload},
    }


def _delta(item_id: str, item_type: str, **payload: Any) -> dict[str, Any]:
    """Build a synthetic ``item/<type>/delta`` notification."""
    return {
        "method": f"item/{item_type}/delta",
        "params": {"itemId": item_id, **payload},
    }


def _completed(item_id: str, **payload: Any) -> dict[str, Any]:
    """Build a synthetic ``item/completed`` notification."""
    return {
        "method": "item/completed",
        "params": {"itemId": item_id, **payload},
    }


# ---------------------------------------------------------------------------
# Agent message
# ---------------------------------------------------------------------------


class TestAgentMessage:
    """``agentMessage`` items → ``{role: "assistant", content}``."""

    @pytest.mark.asyncio
    async def test_simple_message_projects_to_assistant(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="Hello, "),
            _delta("i1", "agentMessage", text="world."),
            _completed("i1", text="Hello, world."),
        ])
        result = projector.finalize_turn()
        assert result.messages == [
            {"role": "assistant", "content": "Hello, world."},
        ]
        # Final text reflects the assistant message.
        assert result.final_text == "Hello, world."
        # No tool iterations on a pure text turn.
        assert result.tool_iterations == 0

    @pytest.mark.asyncio
    async def test_deltas_alone_reconstruct_text_when_no_completed_payload(
        self,
    ) -> None:
        # If Codex's completed event omits the canonical ``text``
        # field, we fall back to the accumulated deltas. Pin this
        # behaviour so a Codex schema regression doesn't silently
        # drop assistant content.
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="part-A"),
            _delta("i1", "agentMessage", text=" part-B"),
            _completed("i1"),  # NO text in completed
        ])
        result = projector.finalize_turn()
        assert result.messages == [
            {"role": "assistant", "content": "part-A part-B"},
        ]

    @pytest.mark.asyncio
    async def test_consecutive_agent_messages_get_paragraph_break(self) -> None:
        """Two separate agentMessage items must be separated by a
        blank line in the live stream. Without this fix, Codex's
        'commentary A → tool → commentary B' pattern renders as one
        un-broken blob because each item's deltas stream raw with no
        boundary marker between distinct messages."""
        captured: list[str] = []

        async def cb(text: str) -> None:
            captured.append(text)

        projector = CodexEventProjector(stream_callback=cb)
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="First message."),
            _completed("i1", text="First message."),
            # Different item id — second message after some tool work.
            _started("i2", "agentMessage"),
            _delta("i2", "agentMessage", text="Second message."),
            _completed("i2", text="Second message."),
        ])

        # First chunk is verbatim (no leading break — nothing to
        # separate from).
        assert captured[0] == "First message."
        # The next item's first delta carries a leading "\n\n" so the
        # chat surface shows the two messages as separate paragraphs.
        assert captured[1].startswith("\n\n")
        assert captured[1] == "\n\nSecond message."

    @pytest.mark.asyncio
    async def test_consecutive_deltas_within_same_item_have_no_break(self) -> None:
        """Within ONE item, deltas must stream without injected
        separators — the model produces continuous prose and a
        spurious break would corrupt sentences mid-word."""
        captured: list[str] = []

        async def cb(text: str) -> None:
            captured.append(text)

        projector = CodexEventProjector(stream_callback=cb)
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="Hello "),
            _delta("i1", "agentMessage", text="world."),
            _completed("i1", text="Hello world."),
        ])

        # No injected "\n\n" anywhere — all chunks pass through clean.
        assert captured == ["Hello ", "world."]

    @pytest.mark.asyncio
    async def test_streams_text_to_callback(self) -> None:
        # Stream callback receives each delta's text in order, so the
        # desktop renderer sees live output instead of waiting for
        # the turn to finish.
        captured: list[str] = []

        async def cb(text: str) -> None:
            captured.append(text)

        projector = CodexEventProjector(stream_callback=cb)
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="chunk1 "),
            _delta("i1", "agentMessage", text="chunk2 "),
            _delta("i1", "agentMessage", text="chunk3"),
            _completed("i1", text="chunk1 chunk2 chunk3"),
        ])
        assert captured == ["chunk1 ", "chunk2 ", "chunk3"]

    @pytest.mark.asyncio
    async def test_stream_callback_failure_does_not_break_projection(
        self,
    ) -> None:
        # If the renderer's WebSocket disconnects mid-stream, the
        # projection should still complete cleanly — the message
        # ends up in the session store; the user just didn't see
        # the live delta.
        async def bad_cb(text: str) -> None:
            raise ConnectionResetError("client disconnected")

        projector = CodexEventProjector(stream_callback=bad_cb)
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="hello"),
            _completed("i1", text="hello"),
        ])
        # Projection still succeeds.
        result = projector.finalize_turn()
        assert result.final_text == "hello"

    @pytest.mark.asyncio
    async def test_last_assistant_message_wins_final_text(self) -> None:
        # A turn can produce multiple assistant messages
        # interleaved with tool calls. The "final response" the
        # parent agent reads is the LAST one — typically the
        # conclusion the model arrived at.
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _completed("i1", text="Reading the file..."),
            _started("i2", "agentMessage"),
            _completed("i2", text="Found the bug. Fixed."),
        ])
        result = projector.finalize_turn()
        assert result.final_text == "Found the bug. Fixed."


# ---------------------------------------------------------------------------
# Reasoning items (encrypted continuity)
# ---------------------------------------------------------------------------


class TestReasoning:
    @pytest.mark.asyncio
    async def test_reasoning_with_encrypted_content_is_captured(self) -> None:
        # Reasoning items don't appear in messages (they're noise to
        # a human reader) but their encrypted_content must be
        # preserved for replay on the next turn.
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("r1", "reasoning"),
            _completed(
                "r1",
                encryptedContent="eyJhbGciOiJFQ0RILUVT...",
                summary="Considering test edge cases",
            ),
        ])
        result = projector.finalize_turn()
        # No message in the user-visible thread.
        assert result.messages == []
        # But the blob is in reasoning_items for continuity replay.
        assert len(result.reasoning_items) == 1
        assert result.reasoning_items[0]["encryptedContent"] == "eyJhbGciOiJFQ0RILUVT..."
        assert result.reasoning_items[0]["summary"] == "Considering test edge cases"

    @pytest.mark.asyncio
    async def test_reasoning_without_encrypted_content_is_skipped(self) -> None:
        # Older Codex versions / models without exposed reasoning
        # emit a reasoning item with only a plaintext summary. Nothing
        # to replay → skip entirely (don't store a useless entry).
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("r1", "reasoning"),
            _completed("r1", summary="Some thoughts"),  # no encryptedContent
        ])
        result = projector.finalize_turn()
        assert result.reasoning_items == []


# ---------------------------------------------------------------------------
# Command execution (exec tool)
# ---------------------------------------------------------------------------


class TestCommandExecution:
    @pytest.mark.asyncio
    async def test_command_execution_emits_tool_pair(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("c1", "commandExecution", command="cat foo.py"),
            _delta("c1", "commandExecution", outputDelta="line 1\n"),
            _delta("c1", "commandExecution", outputDelta="line 2\n"),
            _completed("c1", exitCode=0),
        ])
        result = projector.finalize_turn()

        assert len(result.messages) == 2
        # First message: assistant with tool_call.
        msg0 = result.messages[0]
        assert msg0["role"] == "assistant"
        assert msg0["content"] == ""
        assert len(msg0["tool_calls"]) == 1
        tc = msg0["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "exec"
        assert json.loads(tc["function"]["arguments"]) == {"command": "cat foo.py"}
        # Second message: matching tool result.
        msg1 = result.messages[1]
        assert msg1["role"] == "tool"
        assert msg1["tool_call_id"] == tc["id"]
        assert msg1["name"] == "exec"
        assert msg1["content"] == "line 1\nline 2\n"

        # Tool iteration counter incremented.
        assert result.tool_iterations == 1

    @pytest.mark.asyncio
    async def test_command_execution_records_nonzero_exit_code(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("c1", "commandExecution", command="cat missing"),
            _delta("c1", "commandExecution", outputDelta="cat: missing: No such file\n"),
            _completed("c1", exitCode=1),
        ])
        result = projector.finalize_turn()
        # The tool result must include the non-zero exit code so the
        # model can see the failure.
        tool_msg = result.messages[1]
        assert "exit_code=1" in tool_msg["content"]

    @pytest.mark.asyncio
    async def test_command_execution_codex_0125_aggregated_output(self) -> None:
        """Codex 0.125 ships the full stdout in ``aggregatedOutput``
        on the terminal item/completed (no delta stream for short
        commands). The projector must surface it as the tool result."""
        projector = CodexEventProjector()
        await _feed(projector, [
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "id": "call_pwd",
                        "command": "/bin/zsh -lc pwd",
                        "cwd": "/Users/me/repo",
                        "status": "inProgress",
                        "aggregatedOutput": None,
                        "exitCode": None,
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "id": "call_pwd",
                        "command": "/bin/zsh -lc pwd",
                        "cwd": "/Users/me/repo",
                        "aggregatedOutput": "/Users/me/repo\n",
                        "exitCode": 0,
                        "durationMs": 23,
                        "status": "completed",
                    },
                },
            },
        ])
        result = projector.finalize_turn()
        assert len(result.messages) == 2
        tool_msg = result.messages[1]
        assert tool_msg["content"] == "/Users/me/repo\n"
        assert result.tool_iterations == 1


# ---------------------------------------------------------------------------
# Codex 0.125 agentMessage phase handling
# ---------------------------------------------------------------------------


class TestAgentMessagePhase:
    @pytest.mark.asyncio
    async def test_final_answer_phase_wins_over_commentary(self) -> None:
        """When a turn has a commentary message followed by a
        final_answer, ``final_text`` must reflect the final_answer.

        Without phase awareness, the LAST agentMessage wins
        regardless of role — but Codex emits commentary "thinking out
        loud" messages BEFORE the real answer, so naive LAST-wins
        gives the user nonsense as the agent's reply.
        """
        projector = CodexEventProjector()
        await _feed(projector, [
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "m1",
                        "text": "",
                        "phase": "commentary",
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "m1",
                        "text": "Let me write that file first.",
                        "phase": "commentary",
                    },
                },
            },
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "m2",
                        "text": "",
                        "phase": "final_answer",
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "m2",
                        "text": "Fibonacci script created and verified.",
                        "phase": "final_answer",
                    },
                },
            },
        ])
        result = projector.finalize_turn()
        assert result.final_text == "Fibonacci script created and verified."
        # Both messages still appear in the chat surface so users see
        # the running commentary too.
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_commentary_only_still_becomes_final_text(self) -> None:
        """If no final_answer arrives (interrupted turn?), the last
        commentary message becomes the ``final_text`` fallback —
        better something than nothing."""
        projector = CodexEventProjector()
        await _feed(projector, [
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "m1",
                        "text": "Working on it...",
                        "phase": "commentary",
                    },
                },
            },
        ])
        result = projector.finalize_turn()
        # finalize_turn flushes the open commentary — the assistant
        # message is appended, but final_text stays empty because
        # phase!=final_answer and we have no answer.
        assert len(result.messages) == 1
        # final_text remains empty when only commentary phase items
        # exist — the parent agent reads no final answer, signals to
        # user that codex didn't conclude.
        assert result.final_text == ""


# ---------------------------------------------------------------------------
# File change (apply_patch)
# ---------------------------------------------------------------------------


class TestFileChange:
    @pytest.mark.asyncio
    async def test_file_change_legacy_flat_shape(self) -> None:
        """Backward-compat: older Codex builds shipped path/diff flat."""
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("f1", "fileChange"),
            _completed(
                "f1",
                path="foo.py",
                diff="--- foo.py\n+++ foo.py\n@@ -1 +1 @@\n-old\n+new\n",
            ),
        ])
        result = projector.finalize_turn()

        assert len(result.messages) == 2
        tc = result.messages[0]["tool_calls"][0]
        assert tc["function"]["name"] == "apply_patch"
        args = json.loads(tc["function"]["arguments"])
        assert args["path"] == "foo.py"
        assert "diff" in args
        # Summary names the path so users can see which file changed.
        result_content = result.messages[1]["content"]
        assert "foo.py" in result_content
        assert result.tool_iterations == 1

    @pytest.mark.asyncio
    async def test_file_change_codex_0125_changes_array(self) -> None:
        """Codex 0.125 wire shape: ``changes: [{path, kind, diff}]``.

        item/started carries the full final state for fileChange
        (with status=inProgress); item/completed mostly flips the
        status. We project from the started payload.
        """
        projector = CodexEventProjector()
        diff = 'def fibonacci(n):\n    return n\n'
        notes = [
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "id": "call_abc",
                        "changes": [
                            {
                                "path": "/tmp/fibonacci.py",
                                "kind": {"type": "add"},
                                "diff": diff,
                            },
                        ],
                        "status": "inProgress",
                    },
                    "threadId": "t1",
                    "turnId": "tu1",
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "id": "call_abc",
                        "changes": [
                            {
                                "path": "/tmp/fibonacci.py",
                                "kind": {"type": "add"},
                                "diff": diff,
                            },
                        ],
                        "status": "completed",
                    },
                    "threadId": "t1",
                    "turnId": "tu1",
                },
            },
        ]
        await _feed(projector, notes)
        result = projector.finalize_turn()

        assert len(result.messages) == 2
        tc = result.messages[0]["tool_calls"][0]
        assert tc["function"]["name"] == "apply_patch"
        args = json.loads(tc["function"]["arguments"])
        assert args["path"] == "/tmp/fibonacci.py"
        assert diff in args["diff"]

        result_content = result.messages[1]["content"]
        assert "/tmp/fibonacci.py" in result_content
        assert "add" in result_content
        assert result.tool_iterations == 1

    @pytest.mark.asyncio
    async def test_file_change_started_only_no_completed(self) -> None:
        """If item/completed never arrives, finalize_turn flushes from started.

        Codex 0.125's item/started for fileChange carries the full
        diff already. A dropped or delayed completed event must NOT
        cause the change to be silently lost.
        """
        projector = CodexEventProjector()
        await _feed(projector, [
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "id": "call_xyz",
                        "changes": [
                            {
                                "path": "lonely.py",
                                "kind": {"type": "update"},
                                "diff": "patched",
                            },
                        ],
                        "status": "inProgress",
                    },
                },
            },
        ])
        result = projector.finalize_turn()
        assert len(result.messages) == 2
        assert "lonely.py" in result.messages[1]["content"]
        assert result.tool_iterations == 1


# ---------------------------------------------------------------------------
# MCP tool call
# ---------------------------------------------------------------------------


class TestMcpToolCall:
    @pytest.mark.asyncio
    async def test_mcp_tool_call_uses_server_tool_naming(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("m1", "mcpToolCall"),
            _completed(
                "m1",
                server="filesystem",
                tool="list_directory",
                arguments={"path": "/tmp"},
                result={"entries": ["a.txt", "b.txt"]},
            ),
        ])
        result = projector.finalize_turn()
        tc = result.messages[0]["tool_calls"][0]
        assert tc["function"]["name"] == "mcp.filesystem.list_directory"
        # Result was a dict → serialised for storage.
        assert "a.txt" in result.messages[1]["content"]
        assert result.tool_iterations == 1


# ---------------------------------------------------------------------------
# Dynamic tool call
# ---------------------------------------------------------------------------


class TestDynamicToolCall:
    @pytest.mark.asyncio
    async def test_dynamic_tool_call_uses_payload_name(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("d1", "dynamicToolCall"),
            _completed(
                "d1",
                name="custom_validator",
                arguments={"schema": "foo"},
                result="valid",
            ),
        ])
        result = projector.finalize_turn()
        tc = result.messages[0]["tool_calls"][0]
        assert tc["function"]["name"] == "custom_validator"
        assert result.messages[1]["content"] == "valid"


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_web_search_formats_results(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("w1", "webSearchCall"),
            _completed(
                "w1",
                query="python asyncio",
                results=[
                    {"title": "Asyncio docs", "url": "https://...", "snippet": "Async I/O"},
                    {"title": "Real Python", "url": "https://...", "snippet": "Beginner's guide"},
                ],
            ),
        ])
        result = projector.finalize_turn()
        tc = result.messages[0]["tool_calls"][0]
        assert tc["function"]["name"] == "web_search"
        # Result text formats each hit on its own block.
        content = result.messages[1]["content"]
        assert "Asyncio docs" in content
        assert "Real Python" in content


# ---------------------------------------------------------------------------
# Lifecycle edge cases
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_unknown_item_type_is_skipped_silently(self) -> None:
        # Codex ships a new item type we don't yet know about. The
        # projector must NOT crash — it logs and moves on so the
        # rest of the turn still produces output.
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("u1", "futureItemType"),
            _completed("u1", someField="value"),
            _started("i1", "agentMessage"),
            _completed("i1", text="OK"),
        ])
        result = projector.finalize_turn()
        # The agentMessage still got through.
        assert result.final_text == "OK"

    @pytest.mark.asyncio
    async def test_delta_before_start_is_logged_not_crashed(self) -> None:
        # Codex sends a delta for an item that was never started
        # (replay glitch, etc.). The projector logs and drops it
        # but the rest of the turn continues.
        projector = CodexEventProjector()
        await _feed(projector, [
            _delta("ghost", "agentMessage", text="should be ignored"),
            _started("i1", "agentMessage"),
            _completed("i1", text="real message"),
        ])
        result = projector.finalize_turn()
        assert result.final_text == "real message"

    @pytest.mark.asyncio
    async def test_open_items_finalize_on_turn_end(self) -> None:
        # If turn ends with an item still in mid-stream (codex died,
        # turn interrupted), finalize_turn() flushes whatever buffer
        # state it has — partial message > silently lost content.
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _delta("i1", "agentMessage", text="partial..."),
            # NO completed event for i1
        ])
        result = projector.finalize_turn()
        assert result.final_text == "partial..."

    @pytest.mark.asyncio
    async def test_duplicate_start_is_logged_not_crashed(self) -> None:
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _started("i1", "agentMessage"),  # duplicate
            _delta("i1", "agentMessage", text="text"),
            _completed("i1", text="text"),
        ])
        result = projector.finalize_turn()
        # Single message — the duplicate was tolerated.
        assert len(result.messages) == 1
        assert result.final_text == "text"

    @pytest.mark.asyncio
    async def test_turn_completed_notification_is_informational(self) -> None:
        # The projector doesn't take action on turn/completed —
        # finalize is called explicitly by the session layer.
        projector = CodexEventProjector()
        await _feed(projector, [
            _started("i1", "agentMessage"),
            _completed("i1", text="done"),
            {"method": "turn/completed", "params": {}},
        ])
        # finalize_turn is still required; turn/completed alone
        # doesn't drain buffers.
        result = projector.finalize_turn()
        assert result.final_text == "done"


# ---------------------------------------------------------------------------
# Tool iteration counter
# ---------------------------------------------------------------------------


class TestToolIterationCounter:
    @pytest.mark.asyncio
    async def test_each_mutating_tool_counts_once(self) -> None:
        # The session layer uses this counter to drive the
        # skill-nudge cadence — same heuristic Flowly's native
        # turn loop uses. Counter increments only on mutating /
        # iteration-spending items.
        projector = CodexEventProjector()
        await _feed(projector, [
            # 3 tool-iteration items
            _started("c1", "commandExecution", command="ls"),
            _completed("c1", exitCode=0),
            _started("f1", "fileChange"),
            _completed("f1", path="foo.py", diff=""),
            _started("w1", "webSearchCall"),
            _completed("w1", query="x", results=[]),
            # plain text items don't count
            _started("a1", "agentMessage"),
            _completed("a1", text="done"),
            _started("r1", "reasoning"),
            _completed("r1", encryptedContent="abc"),
        ])
        result = projector.finalize_turn()
        assert result.tool_iterations == 3


# ---------------------------------------------------------------------------
# Tool call id stability
# ---------------------------------------------------------------------------


class TestToolCallIdStability:
    @pytest.mark.asyncio
    async def test_tool_call_id_is_stable_across_reprojection(self) -> None:
        # Re-projecting the same notification stream should produce
        # byte-identical messages. The session layer relies on this
        # for idempotent message appending (Codex sometimes
        # re-streams a turn during reconnect).
        events = [
            _started("c1", "commandExecution", command="echo hi"),
            _delta("c1", "commandExecution", outputDelta="hi\n"),
            _completed("c1", exitCode=0),
        ]
        result_a = (await _project(events)).messages
        result_b = (await _project(events)).messages
        # Both projections produce identical tool_call_ids.
        id_a = result_a[0]["tool_calls"][0]["id"]
        id_b = result_b[0]["tool_calls"][0]["id"]
        assert id_a == id_b
        assert id_a.startswith("codex_commandExecution_c1")


async def _project(events: list[dict[str, Any]]) -> TurnProjection:
    projector = CodexEventProjector()
    await _feed(projector, events)
    return projector.finalize_turn()
