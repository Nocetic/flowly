"""Tests for the ``codex_session`` Flowly tool wrapper.

These verify the integration boundary between Flowly's agent loop
and the Codex runtime: session-key plumbing, metadata persistence
(``codex_thread_id``, ``codex_reasoning_items``), message injection
into the live session, retire-on-failure semantics, action='new'
reset.

We mock ``CodexSession`` itself (its lifecycle is covered by
:mod:`tests.test_codex_session`) so these tests focus on the tool's
contract with the host agent loop.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from flowly.agent.tools.codex_session import (
    CodexSessionTool,
)
from flowly.codex.session import (
    CodexSession,
    CodexSessionConfig,
    TurnResult,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCodexSession:
    """Stub :class:`CodexSession` that returns scripted TurnResults.

    Tests construct one of these, hand it to the tool via the
    session_store getter, and inspect what state the tool reads /
    writes.
    """

    def __init__(self) -> None:
        self.run_turn_calls: list[str] = []
        self.scripted_results: list[TurnResult] = []
        self.set_thread_id_calls: list[str | None] = []
        self.set_initial_reasoning_items_calls: list[list[dict[str, Any]]] = []
        self.close_called = False
        self._retired = False
        self.reasoning_items: list[dict[str, Any]] = []

    def script(self, result: TurnResult) -> None:
        self.scripted_results.append(result)

    async def run_turn(self, user_input, *, stream_callback=None):
        self.run_turn_calls.append(user_input)
        if not self.scripted_results:
            return TurnResult(thread_id="thr_default", final_text="(empty)")
        return self.scripted_results.pop(0)

    def set_thread_id(self, thread_id):
        self.set_thread_id_calls.append(thread_id)

    def set_initial_reasoning_items(self, items):
        self.set_initial_reasoning_items_calls.append(items)

    @property
    def retired(self):
        return self._retired

    async def close(self):
        self.close_called = True
        self._retired = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_factory():
    """Build a tool wired against in-memory host state.

    Returns ``(tool, host_state)``. ``host_state`` is a dict of the
    test-visible accessor backing storage so tests can inspect
    metadata writes / session installs.
    """
    def build(
        *,
        initial_metadata: dict[str, Any] | None = None,
        active_session_key: str = "telegram:12345",
        codex_session_factory=None,  # lazy factory for the stub
    ):
        host_state: dict[str, Any] = {
            "metadata": {active_session_key: dict(initial_metadata or {})},
            "stored_session": {},  # session_key -> FakeCodexSession
            "stream_resolver_call_count": 0,
            "active_session_key": active_session_key,
        }

        def session_accessor(sk):
            return host_state["metadata"].setdefault(sk, {})

        def stream_resolver(sk):
            host_state["stream_resolver_call_count"] += 1
            return None  # tests don't exercise streaming here

        def session_store_get(sk):
            return host_state["stored_session"].get(sk)

        def session_store_set(sk, sess):
            if sess is None:
                host_state["stored_session"].pop(sk, None)
            else:
                host_state["stored_session"][sk] = sess

        config = CodexSessionConfig(codex_bin="codex-stub")
        tool = CodexSessionTool(
            config=config,
            session_accessor=session_accessor,
            stream_resolver=stream_resolver,
            session_store_get=session_store_get,
            session_store_set=session_store_set,
            active_session_key_getter=lambda: host_state["active_session_key"],
        )

        # Replace _build_codex_session so it returns the test's stub
        # rather than spawning a real CodexSession.
        def fake_build(*, metadata, cwd_override):
            stub = FakeCodexSession()
            if codex_session_factory is not None:
                codex_session_factory(stub, metadata)
            return stub

        tool._build_codex_session = fake_build  # type: ignore[method-assign]

        return tool, host_state

    return build


# ---------------------------------------------------------------------------
# Successful first turn
# ---------------------------------------------------------------------------


class TestFirstTurn:
    @pytest.mark.asyncio
    async def test_first_turn_creates_session_and_persists_thread_id(
        self, tool_factory,
    ) -> None:
        result_holder: dict[str, Any] = {}

        def factory(stub: FakeCodexSession, metadata):
            stub.script(TurnResult(
                thread_id="thr_first",
                final_text="Done.",
                messages=[
                    {"role": "assistant", "content": "Done."},
                ],
                tool_iterations=0,
            ))
            result_holder["stub"] = stub

        tool, state = tool_factory(codex_session_factory=factory)

        result_json = await tool.execute(task="fix the bug")
        result = json.loads(result_json)

        assert result["status"] == "ok"
        assert result["thread_id"] == "thr_first"
        assert result["final_text"] == "Done."

        # thread_id persisted on metadata.
        meta = state["metadata"]["telegram:12345"]
        assert meta["codex_thread_id"] == "thr_first"

        # Stub session was stored.
        assert state["stored_session"]["telegram:12345"] is result_holder["stub"]

        # Stub's run_turn was called with the verbatim task.
        assert result_holder["stub"].run_turn_calls == ["fix the bug"]

    @pytest.mark.asyncio
    async def test_messages_are_queued_for_session_injection(
        self, tool_factory,
    ) -> None:
        messages_to_inject = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "x", "name": "exec", "content": "stdout"},
            {"role": "assistant", "content": "Done."},
        ]

        def factory(stub: FakeCodexSession, metadata):
            stub.script(TurnResult(
                thread_id="thr_m",
                final_text="Done.",
                messages=messages_to_inject,
            ))

        tool, state = tool_factory(codex_session_factory=factory)
        await tool.execute(task="x")

        # Pending queue contains the three Codex messages, in order.
        queued = state["metadata"]["telegram:12345"]["codex_pending_messages"]
        assert queued == messages_to_inject


# ---------------------------------------------------------------------------
# Resume — subsequent turn reuses thread_id
# ---------------------------------------------------------------------------


class TestResume:
    @pytest.mark.asyncio
    async def test_existing_thread_id_is_loaded_into_new_session(
        self, tool_factory,
    ) -> None:
        # Session was previously created (with retired session, so a
        # rebuild is needed) but thread_id is already on metadata.
        # The tool must seed the new CodexSession with the persisted
        # thread_id + reasoning items so Codex resumes the thread.
        captured_metadata: dict[str, Any] = {}

        def factory(stub: FakeCodexSession, metadata):
            captured_metadata.update(metadata)
            stub.script(TurnResult(thread_id="thr_resumed", final_text="continuing..."))

        tool, state = tool_factory(
            initial_metadata={
                "codex_thread_id": "thr_resumed",
                "codex_reasoning_items": [
                    {"encryptedContent": "OLD", "itemId": "r0"},
                ],
            },
            codex_session_factory=factory,
        )
        # Note: tool._build_codex_session is monkeypatched to call
        # our factory but doesn't actually seed the stub with
        # thread_id / reasoning. We assert here that metadata has
        # the values the real _build_codex_session would have read.
        await tool.execute(task="continue")
        assert captured_metadata["codex_thread_id"] == "thr_resumed"
        assert captured_metadata["codex_reasoning_items"][0]["encryptedContent"] == "OLD"


# ---------------------------------------------------------------------------
# action='new' — explicit reset
# ---------------------------------------------------------------------------


class TestActionNew:
    @pytest.mark.asyncio
    async def test_action_new_clears_thread_id_and_closes_old_session(
        self, tool_factory,
    ) -> None:
        # Pre-existing session with a thread_id; the user explicitly
        # asks for a fresh start.
        old_stub = FakeCodexSession()
        old_stub.script(TurnResult())  # not used (we close instead)

        new_stub_holder: dict[str, FakeCodexSession] = {}

        def factory(stub: FakeCodexSession, metadata):
            stub.script(TurnResult(
                thread_id="thr_new_x",
                final_text="fresh start",
            ))
            new_stub_holder["stub"] = stub

        tool, state = tool_factory(
            initial_metadata={
                "codex_thread_id": "thr_old",
                "codex_reasoning_items": [{"encryptedContent": "OLD"}],
            },
            codex_session_factory=factory,
        )
        # Install the old session.
        state["stored_session"]["telegram:12345"] = old_stub

        result_json = await tool.execute(task="new project", action="new")
        result = json.loads(result_json)
        assert result["status"] == "ok"
        assert result["thread_id"] == "thr_new_x"

        # Old session was closed.
        assert old_stub.close_called is True
        # Metadata was cleared (thread_id is now the NEW one).
        meta = state["metadata"]["telegram:12345"]
        assert meta["codex_thread_id"] == "thr_new_x"
        # codex_reasoning_items was cleared on the reset (the new
        # session is empty until it produces fresh reasoning items).
        # The "new" stub has empty reasoning_items so it shouldn't be
        # re-written either.
        assert "codex_reasoning_items" not in meta


# ---------------------------------------------------------------------------
# Retire on failure
# ---------------------------------------------------------------------------


class TestRetireOnFailure:
    @pytest.mark.asyncio
    async def test_should_retire_closes_session_keeps_thread(
        self, tool_factory,
    ) -> None:
        # Codex turn failed with should_retire=True (e.g. wedge,
        # OAuth refresh). Tool must close the session but PRESERVE
        # thread_id so the next call can resume.
        def factory(stub: FakeCodexSession, metadata):
            stub.script(TurnResult(
                thread_id="thr_w",
                error="post-tool wedge",
                should_retire=True,
                interrupted=True,
                final_text="partial work",
            ))

        tool, state = tool_factory(codex_session_factory=factory)
        result_json = await tool.execute(task="long task")
        result = json.loads(result_json)
        assert result["status"] == "error"
        assert "wedge" in result["error"].lower()
        # final_text salvaged from partial turn.
        assert result["final_text"] == "partial work"

        # Session was retired (removed from store).
        assert state["stored_session"].get("telegram:12345") is None
        # But thread_id is preserved on metadata for resume.
        assert state["metadata"]["telegram:12345"]["codex_thread_id"] == "thr_w"

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_actionable_hint(
        self, tool_factory,
    ) -> None:
        def factory(stub: FakeCodexSession, metadata):
            stub.script(TurnResult(
                error="codex binary not found at 'codex'. Install via `npm i -g @openai/codex`",
                should_retire=True,
            ))

        tool, state = tool_factory(codex_session_factory=factory)
        result_json = await tool.execute(task="x")
        result = json.loads(result_json)
        assert result["status"] == "error"
        # Hint maps the error to an install command.
        assert "hint" in result
        assert "npm i -g @openai/codex" in result["hint"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_empty_task_returns_error_without_calling_codex(
        self, tool_factory,
    ) -> None:
        called = {"count": 0}

        def factory(stub, metadata):
            called["count"] += 1

        tool, state = tool_factory(codex_session_factory=factory)
        result_json = await tool.execute(task="")
        result = json.loads(result_json)
        assert result["status"] == "error"
        assert "task" in result["error"].lower()
        # No CodexSession constructed.
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_whitespace_task_treated_as_empty(self, tool_factory) -> None:
        tool, state = tool_factory()
        result_json = await tool.execute(task="   \n\t  ")
        assert json.loads(result_json)["status"] == "error"


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------


class TestToolSchema:
    def test_schema_has_required_task_field(self) -> None:
        from flowly.agent.tools.codex_session import CodexSessionTool
        # Build a minimal tool just to inspect schema.
        tool = CodexSessionTool(
            config=CodexSessionConfig(),
            session_accessor=lambda sk: {},
            stream_resolver=lambda sk: None,
            session_store_get=lambda sk: None,
            session_store_set=lambda sk, s: None,
        )
        schema = tool.to_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "codex_session"
        params = fn["parameters"]
        assert "task" in params["required"]
        # action defaults to "ask" and has the right enum.
        action = params["properties"]["action"]
        assert set(action["enum"]) == {"ask", "new"}

    def test_description_mentions_requirements_and_continuity(self) -> None:
        tool = CodexSessionTool(
            config=CodexSessionConfig(),
            session_accessor=lambda sk: {},
            stream_resolver=lambda sk: None,
            session_store_get=lambda sk: None,
            session_store_set=lambda sk, s: None,
        )
        desc = tool.description.lower()
        # Description must tell the model about Codex CLI dependency
        # and the session-continuity contract.
        assert "codex cli" in desc or "@openai/codex" in desc
        assert "thread" in desc or "session" in desc


# ---------------------------------------------------------------------------
# No active session safety
# ---------------------------------------------------------------------------


class TestNoActiveSession:
    @pytest.mark.asyncio
    async def test_missing_session_key_returns_error(
        self, tool_factory,
    ) -> None:
        tool, state = tool_factory(active_session_key="")
        # Defensive — the loop normally ensures a session_key is set,
        # but if a test or rogue caller exercises the tool with no
        # active session, we should fail cleanly.
        result_json = await tool.execute(task="anything")
        assert json.loads(result_json)["status"] == "error"


# ---------------------------------------------------------------------------
# cwd validation — tilde expansion + existence check
# ---------------------------------------------------------------------------


class TestCwdValidation:
    """Field bug (2026-06-11): models pass cwd="~/project"; the OS never
    expands the tilde, the subprocess spawn raised FileNotFoundError, and
    the error was misreported as "codex binary not found"."""

    @pytest.mark.asyncio
    async def test_nonexistent_cwd_returns_clear_error(self, tool_factory) -> None:
        tool, _ = tool_factory()
        result = json.loads(await tool.execute(
            task="inspect", cwd="~/definitely-not-a-real-dir-xyz123",
        ))
        assert result["status"] == "error"
        assert "working directory does not exist" in result["error"]
        # The expanded (absolute) path is shown, not the raw tilde form.
        assert "~" not in result["error"].split(": ", 1)[1]

    @pytest.mark.asyncio
    async def test_tilde_cwd_is_expanded_before_build(self, tool_factory) -> None:
        import os
        seen: dict[str, Any] = {}
        tool, _ = tool_factory()
        original_build = tool._build_codex_session

        def capture_build(*, metadata, cwd_override):
            seen["cwd"] = cwd_override
            return original_build(metadata=metadata, cwd_override=cwd_override)

        tool._build_codex_session = capture_build  # type: ignore[method-assign]
        await tool.execute(task="inspect", cwd="~")
        assert seen["cwd"] == os.path.expanduser("~")
