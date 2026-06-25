"""Tests for the Codex session layer.

The session owns the turn lifecycle: spawn the transport, thread/start
or turn/start, poll the notification stream, handle approval requests,
fire wedge-detection watchdogs, finalize the projection. These tests
mock the underlying :class:`CodexAppServerClient` so we can drive
sessions through every interesting state without spawning real
subprocesses.

What we pin:

  * First turn issues ``thread/start``, subsequent turns issue
    ``turn/start`` and reuse the saved thread_id.
  * Reasoning continuity blobs flow forward from turn N to turn N+1.
  * Notifications drive the projector and produce TurnResult fields.
  * ``turn/completed`` ends the loop cleanly.
  * Subprocess exit mid-turn fails the result, sets should_retire.
  * OAuth-failure stderr hints upgrade generic errors to actionable.
  * Hard turn deadline → interrupt + retire.
  * Post-tool wedge timeout → interrupt + retire (but only after a
    tool iteration has actually happened).
  * Server-initiated requests are answered (default: decline) so
    Codex's loop doesn't block on them.
  * Approval callback gets routed correctly when configured.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.codex.app_server import (
    CodexProtocolError,
    CodexRPCError,
    CodexSpawnError,
)
from flowly.codex.session import (
    CodexSession,
    CodexSessionConfig,
    TurnResult,
    classify_oauth_failure,
)


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


class FakeCodexClient:
    """Hand-rolled async mock for :class:`CodexAppServerClient`.

    Lets tests script the exact sequence of responses, notifications,
    server-requests, and stderr lines a session encounters. Avoids
    spinning up real subprocesses for what is mostly state-machine
    logic.

    Scripting model:
        * ``script_response(method, result)`` queues a response for
          the next matching request.
        * ``script_error(method, code, message)`` queues an error
          response.
        * ``script_notification(method, params)`` queues a
          notification that ``take_notification`` will return.
        * ``script_server_request(req_id, method, params)`` queues a
          server-initiated request.
        * ``script_die()`` schedules the next ``is_alive`` check to
          return False.
        * ``script_stderr(line)`` appends to the stderr tail buffer.
    """

    def __init__(self) -> None:
        # Per-method response queues. Tests usually script one
        # response per request; if a method is called more often
        # we surface the last entry.
        self._responses: dict[str, list[Any]] = {}
        self._errors: dict[str, list[CodexRPCError]] = {}
        # Sequential queues — order matters.
        self._notifications: list[dict[str, Any]] = []
        self._server_requests: list[dict[str, Any]] = []
        # Captured outbound traffic so tests can assert on what
        # the session said.
        self.requests: list[tuple[str, dict[str, Any] | None, float | None]] = []
        self.responses_sent: list[tuple[int, Any]] = []
        self.errors_sent: list[tuple[int, int, str]] = []
        self.notifications_sent: list[tuple[str, dict[str, Any] | None]] = []
        # Health.
        self._alive: bool = True
        self._exit_code: int | None = None
        self._stderr: list[str] = []
        self.closed: bool = False

    # -- scripting --

    def script_response(self, method: str, result: Any) -> None:
        self._responses.setdefault(method, []).append(result)

    def script_error(self, method: str, code: int, message: str, data: Any = None) -> None:
        self._errors.setdefault(method, []).append(CodexRPCError(code, message, data))

    def script_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._notifications.append({"method": method, "params": params or {}})

    def script_server_request(
        self, req_id: int, method: str, params: dict[str, Any] | None = None,
    ) -> None:
        self._server_requests.append({
            "id": req_id, "method": method, "params": params or {},
        })

    def script_die(self, exit_code: int = 0) -> None:
        self._alive = False
        self._exit_code = exit_code

    def script_stderr(self, line: str) -> None:
        self._stderr.append(line)

    # -- transport surface (mimics CodexAppServerClient) --

    async def request(
        self, method: str, params: dict[str, Any] | None = None,
        *, timeout: float | None = None,
    ) -> Any:
        self.requests.append((method, params, timeout))
        # Errors take priority over responses if both are scripted.
        if self._errors.get(method):
            raise self._errors[method].pop(0)
        if self._responses.get(method):
            return self._responses[method].pop(0)
        # Default empty result.
        return {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.notifications_sent.append((method, params))

    async def respond(self, request_id: int, result: Any) -> None:
        self.responses_sent.append((request_id, result))

    async def respond_error(self, request_id: int, code: int, message: str, data: Any = None) -> None:
        self.errors_sent.append((request_id, code, message))

    async def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        if self._notifications:
            return self._notifications.pop(0)
        return None

    async def take_server_request(self, timeout: float = 0.0) -> dict[str, Any] | None:
        if self._server_requests:
            return self._server_requests.pop(0)
        return None

    def is_alive(self) -> bool:
        return self._alive

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def stderr_tail(self, n: int = 50) -> list[str]:
        tail = list(self._stderr)
        if n <= 0 or n >= len(tail):
            return tail
        return tail[-n:]

    async def close(self) -> int | None:
        self.closed = True
        self._alive = False
        return self._exit_code


@pytest.fixture
def session_with_fake_client(monkeypatch):
    """Build a CodexSession whose underlying transport is a FakeCodexClient.

    The fake replaces the real subprocess so tests can script
    notifications and responses deterministically.
    """
    def _factory(approval_callback=None) -> tuple[CodexSession, FakeCodexClient]:
        fake = FakeCodexClient()
        config = CodexSessionConfig(
            codex_bin="codex-stub",
            turn_timeout_s=5.0,
            post_tool_quiet_timeout_s=2.0,
        )
        session = CodexSession(
            config=config,
            approval_callback=approval_callback,
        )

        # Monkey-patch ensure_client to return the fake instead of
        # spawning a real subprocess. ``session._client`` is set
        # so subsequent calls reuse it (matches real lazy-spawn).
        async def fake_ensure_client():
            session._client = fake  # type: ignore[assignment]
            return fake

        monkeypatch.setattr(session, "ensure_client", fake_ensure_client)
        return session, fake

    return _factory


# ---------------------------------------------------------------------------
# OAuth classification
# ---------------------------------------------------------------------------


class TestOAuthClassification:
    """``classify_oauth_failure`` upgrades generic transport errors
    into a user-actionable hint when Codex's stderr / error payload
    contains a known OAuth-failure substring."""

    def test_invalid_grant_in_stderr(self) -> None:
        hint = classify_oauth_failure(
            "Error: token refresh failed: invalid_grant",
        )
        assert hint is not None
        assert "expired" in hint.lower() or "login" in hint.lower()

    def test_401_unauthorized_detected(self) -> None:
        hint = classify_oauth_failure("HTTP 401 Unauthorized")
        assert hint is not None

    def test_unknown_error_returns_none(self) -> None:
        hint = classify_oauth_failure("disk full")
        assert hint is None

    def test_empty_returns_none(self) -> None:
        assert classify_oauth_failure() is None
        assert classify_oauth_failure("", None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# First turn — thread/start
# ---------------------------------------------------------------------------


class TestFirstTurn:
    @pytest.mark.asyncio
    async def test_first_turn_issues_thread_start_then_turn_start(
        self, session_with_fake_client,
    ) -> None:
        # Codex protocol splits thread creation and turn execution.
        # First turn: thread/start (no input) → turn/start (with input).
        # Subsequent turns: just turn/start.
        session, fake = session_with_fake_client()
        fake.script_response("thread/start", {"threadId": "thr_new"})
        fake.script_response("turn/start", {"turn": {"id": "trn_new"}})
        fake.script_notification(
            "item/started",
            {"itemId": "i1", "type": "agentMessage"},
        )
        fake.script_notification(
            "item/completed",
            {"itemId": "i1", "text": "hello"},
        )
        fake.script_notification("turn/completed", {})

        result = await session.run_turn("hi codex")

        # Both RPCs went out, in order.
        methods = [r[0] for r in fake.requests]
        assert methods.index("thread/start") < methods.index("turn/start"), (
            f"expected thread/start before turn/start, got {methods}"
        )

        # thread/start carries NO ``input`` — Codex silently swallows
        # turns on inputs in thread/start. Pin this so a regression
        # putting input back into thread/start fails loudly.
        thread_start_call = next(r for r in fake.requests if r[0] == "thread/start")
        assert "input" not in (thread_start_call[1] or {}), (
            "thread/start must NOT carry an 'input' field — Codex ignores "
            "the turn when it does"
        )

        # turn/start carries the input.
        turn_start_call = next(r for r in fake.requests if r[0] == "turn/start")
        params = turn_start_call[1] or {}
        assert "input" in params
        assert params.get("threadId") == "thr_new"

        # Adopted the thread id for future turns.
        assert session.thread_id == "thr_new"

        # Projection produced the message.
        assert result.final_text == "hello"
        assert not result.should_retire

    @pytest.mark.asyncio
    async def test_first_turn_accepts_nested_thread_object(
        self, session_with_fake_client,
    ) -> None:
        # Codex 2026-05+ wraps the id under ``thread``. Same outcome
        # as the flat ``threadId`` shape; the session unpacks both.
        session, fake = session_with_fake_client()
        fake.script_response(
            "thread/start",
            {
                "thread": {
                    "id": "019e386c-e19e-7d62-ac11-d93e1380d130",
                    "forkedFromId": None,
                    "preview": "",
                },
            },
        )
        fake.script_notification("turn/completed", {})

        result = await session.run_turn("hi codex")
        assert session.thread_id == "019e386c-e19e-7d62-ac11-d93e1380d130"
        assert not result.should_retire

    @pytest.mark.asyncio
    async def test_first_turn_falls_back_to_flat_id_field(
        self, session_with_fake_client,
    ) -> None:
        # Oldest Codex builds returned ``{"id": "..."}`` directly.
        # We accept that as a last-resort fallback so a downgrade
        # to an old binary doesn't break the integration entirely.
        session, fake = session_with_fake_client()
        fake.script_response("thread/start", {"id": "thr_legacy"})
        fake.script_notification("turn/completed", {})

        await session.run_turn("hi")
        assert session.thread_id == "thr_legacy"

    @pytest.mark.asyncio
    async def test_thread_start_failure_retires_session(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        fake.script_error("thread/start", -32000, "bad thread")

        result = await session.run_turn("hi")
        assert result.should_retire is True
        assert result.error is not None
        assert "Codex error" in result.error or "bad thread" in result.error

    @pytest.mark.asyncio
    async def test_thread_start_oauth_failure_upgrades_error(
        self, session_with_fake_client,
    ) -> None:
        # Codex's thread/start returns a 401-ish error with the
        # OAuth pattern in the message. The session should upgrade
        # the generic transport error into a "your login is expired"
        # hint the user can act on.
        session, fake = session_with_fake_client()
        fake.script_error(
            "thread/start", -32000, "401 unauthorized: token has expired",
        )

        result = await session.run_turn("hi")
        assert result.should_retire is True
        assert result.error is not None
        assert "expired" in result.error.lower() or "login" in result.error.lower()


# ---------------------------------------------------------------------------
# Subsequent turns — turn/start, reasoning continuity
# ---------------------------------------------------------------------------


class TestSubsequentTurns:
    @pytest.mark.asyncio
    async def test_second_turn_uses_turn_start(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        # First turn — initialise the thread (thread/start + turn/start).
        fake.script_response("thread/start", {"threadId": "thr_001"})
        fake.script_response("turn/start", {"turn": {"id": "trn_1"}})
        fake.script_notification("item/started", {"itemId": "i1", "type": "agentMessage"})
        fake.script_notification("item/completed", {"itemId": "i1", "text": "t1"})
        fake.script_notification("turn/completed", {})
        await session.run_turn("turn one")

        # Second turn — must reuse the thread, ONLY turn/start.
        fake.script_response("turn/start", {"turn": {"id": "trn_2"}})
        fake.script_notification("item/started", {"itemId": "j1", "type": "agentMessage"})
        fake.script_notification("item/completed", {"itemId": "j1", "text": "t2"})
        fake.script_notification("turn/completed", {})
        result2 = await session.run_turn("turn two")

        methods = [r[0] for r in fake.requests]
        # thread/start exactly once (only on first turn).
        assert methods.count("thread/start") == 1
        # turn/start TWICE — once per turn including the first.
        assert methods.count("turn/start") == 2

        # Thread id unchanged.
        assert session.thread_id == "thr_001"
        assert result2.final_text == "t2"

    @pytest.mark.asyncio
    async def test_reasoning_items_replay_on_subsequent_turn(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()

        # First turn produces a reasoning item.
        fake.script_response("thread/start", {"threadId": "thr_r1"})
        fake.script_response("turn/start", {"turn": {"id": "trn_1"}})
        fake.script_notification("item/started", {"itemId": "r1", "type": "reasoning"})
        fake.script_notification(
            "item/completed",
            {"itemId": "r1", "encryptedContent": "BLOB_FROM_TURN_1"},
        )
        fake.script_notification("turn/completed", {})
        result1 = await session.run_turn("turn one")
        assert any(r["encryptedContent"] == "BLOB_FROM_TURN_1" for r in result1.reasoning_items)

        # Second turn. The session should ship the saved blob back
        # on turn/start's ``input`` field.
        fake.script_response("turn/start", {"turn": {"id": "trn_2"}})
        fake.script_notification("turn/completed", {})
        await session.run_turn("turn two")

        # Inspect the SECOND turn/start call (not the first turn's).
        turn_starts = [r for r in fake.requests if r[0] == "turn/start"]
        assert len(turn_starts) == 2, (
            f"expected 2 turn/start calls (one per turn), got {len(turn_starts)}"
        )
        second_params = turn_starts[1][1] or {}
        input_items = second_params.get("input") or []
        # First item is the replayed reasoning blob, last is the new user prompt.
        assert any(
            item.get("type") == "reasoning" and item.get("encryptedContent") == "BLOB_FROM_TURN_1"
            for item in input_items
        )
        # Codex 2026-05+ expects ``type: "text"`` for the user prompt.
        # Older docs said ``input_text``; the live binary silently
        # ignores it (turn never produces item/* notifications), so
        # the session was switched to ``type: "text"`` to match the
        # reference client.
        assert input_items[-1] == {"type": "text", "text": "turn two"}

    @pytest.mark.asyncio
    async def test_set_thread_id_resumes_persisted_thread(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        # Adopt a thread id from "Flowly session metadata".
        session.set_thread_id("thr_persisted")
        session.set_initial_reasoning_items([
            {"encryptedContent": "OLD_BLOB", "itemId": "r0"},
        ])

        fake.script_response("turn/start", {})
        fake.script_notification("turn/completed", {})
        await session.run_turn("resumed turn")

        methods = [r[0] for r in fake.requests]
        # No thread/start — resume via turn/start directly.
        assert "thread/start" not in methods
        assert "turn/start" in methods

        # The persisted reasoning blob is replayed on the new turn.
        turn_start = next(r for r in fake.requests if r[0] == "turn/start")
        params = turn_start[1] or {}
        input_items = params.get("input") or []
        assert any(
            item.get("type") == "reasoning" and item.get("encryptedContent") == "OLD_BLOB"
            for item in input_items
        )


# ---------------------------------------------------------------------------
# Wedge detection / interrupt
# ---------------------------------------------------------------------------


class TestWedgeDetection:
    @pytest.mark.asyncio
    async def test_hard_turn_timeout_triggers_interrupt(
        self, session_with_fake_client,
    ) -> None:
        # Very short turn timeout (50ms) — the loop never sees
        # turn/completed, so the hard timeout should fire.
        session, fake = session_with_fake_client()
        session._config.turn_timeout_s = 0.05  # type: ignore[attr-defined]
        fake.script_response("thread/start", {"threadId": "thr_t"})
        # Stream a couple of non-terminal notifications so the loop
        # actually polls; never queue turn/completed.
        for _ in range(5):
            fake.script_notification("$/heartbeat", {})

        result = await session.run_turn("hang please")
        assert result.interrupted is True
        assert result.should_retire is True
        assert "deadline" in (result.error or "").lower() or "timeout" in (result.error or "").lower()
        # The session must have issued a turn/interrupt.
        methods = [r[0] for r in fake.requests]
        assert "turn/interrupt" in methods

    @pytest.mark.asyncio
    async def test_subprocess_death_during_turn_retires_session(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        fake.script_response("thread/start", {"threadId": "thr_d"})
        # No notifications scripted; instead mark subprocess dead so
        # the loop sees is_alive() == False on the next iteration.
        fake.script_die(exit_code=1)
        fake.script_stderr("Codex panicked: invalid_grant")

        result = await session.run_turn("anything")
        assert result.should_retire is True
        assert result.error is not None
        # OAuth signal in stderr → upgraded hint.
        assert "expired" in result.error.lower() or "login" in result.error.lower()


# ---------------------------------------------------------------------------
# Server-initiated requests (approval flow)
# ---------------------------------------------------------------------------


class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_unhandled_server_request_is_declined_by_default(
        self, session_with_fake_client,
    ) -> None:
        # Codex's approval-request contract expects a
        # ``{"decision": ...}`` result, NOT a JSON-RPC error. Sending
        # an error leaves the turn in a confused state where it
        # neither continues nor terminates. The session was switched
        # to respond_result with ``decision: "decline"``.
        session, fake = session_with_fake_client()
        fake.script_response("thread/start", {"threadId": "thr_a"})
        fake.script_server_request(
            99,
            "item/commandExecution/requestApproval",
            {"command": "rm -rf /tmp"},
        )
        fake.script_notification("turn/completed", {})

        await session.run_turn("do dangerous thing")
        # The session replied with respond (not respond_error).
        assert fake.responses_sent
        req_id, result = fake.responses_sent[0]
        assert req_id == 99
        assert result == {"decision": "decline"}
        # And no error envelope was sent.
        assert not fake.errors_sent

    @pytest.mark.asyncio
    async def test_approval_callback_is_routed(
        self, session_with_fake_client,
    ) -> None:
        captured: list[dict[str, Any]] = []

        async def callback(req):
            captured.append(req)
            return {"decision": "approved"}

        session, fake = session_with_fake_client(approval_callback=callback)
        fake.script_response("thread/start", {"threadId": "thr_a"})
        fake.script_server_request(7, "approval/request", {"command": "ls"})
        fake.script_notification("turn/completed", {})

        await session.run_turn("ok")
        assert captured == [{"method": "approval/request", "params": {"command": "ls"}}]
        # Session responded with the approved decision (not an error).
        assert fake.responses_sent and fake.responses_sent[0][1] == {"decision": "approved"}


# ---------------------------------------------------------------------------
# Spawn failure
# ---------------------------------------------------------------------------


class TestSpawnFailure:
    @pytest.mark.asyncio
    async def test_codex_not_installed_retires_immediately(self) -> None:
        # Real ensure_client path: spawn against a nonexistent binary
        # surfaces CodexSpawnError; the session must turn that into
        # a clean retire-flag, not crash.
        config = CodexSessionConfig(codex_bin="/no/such/codex/bin")
        session = CodexSession(config=config)
        result = await session.run_turn("hi")
        assert result.should_retire is True
        assert result.error is not None
        assert "codex binary" in result.error.lower() or "not found" in result.error.lower()


# ---------------------------------------------------------------------------
# Tool iteration counter pass-through
# ---------------------------------------------------------------------------


class TestToolIterationPropagation:
    @pytest.mark.asyncio
    async def test_tool_iterations_surfaces_to_result(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        fake.script_response("thread/start", {"threadId": "thr_ti"})
        # One exec command.
        fake.script_notification(
            "item/started",
            {"itemId": "c1", "type": "commandExecution", "command": "echo hi"},
        )
        fake.script_notification(
            "item/completed",
            {"itemId": "c1", "output": "hi", "exitCode": 0},
        )
        # One file change.
        fake.script_notification("item/started", {"itemId": "f1", "type": "fileChange"})
        fake.script_notification(
            "item/completed",
            {"itemId": "f1", "path": "foo.py", "diff": "<diff>"},
        )
        fake.script_notification("turn/completed", {})

        result = await session.run_turn("do stuff")
        # Two tool iterations.
        assert result.tool_iterations == 2
        # Each produced an assistant tool_call + tool result pair.
        # 4 total tool-pair messages.
        tool_pair_count = sum(
            1 for m in result.messages if m.get("role") == "tool"
        )
        assert tool_pair_count == 2


# ---------------------------------------------------------------------------
# Close lifecycle
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_is_idempotent_and_retires(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        # Force lazy client creation
        await session.ensure_client()
        await session.close()
        assert session.retired is True
        # Second close doesn't raise.
        await session.close()

    @pytest.mark.asyncio
    async def test_run_turn_after_close_returns_retire_failure(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        await session.ensure_client()
        await session.close()
        # Now run_turn should NOT spawn a new client; the retired
        # flag short-circuits.
        result = await session.run_turn("anything")
        assert result.should_retire is True
        assert result.error is not None


class TestStaleThreadRecovery:
    """A persisted thread id can go stale (codex restart / different process).

    turn/start then fails "thread not found"; the session should drop the
    dead thread, start a fresh one, and retry — not surface the error.
    """

    @pytest.mark.asyncio
    async def test_resumed_thread_missing_starts_fresh(
        self, session_with_fake_client,
    ) -> None:
        session, fake = session_with_fake_client()
        session.set_thread_id("old-thread")  # resuming a persisted thread
        fake.script_error("turn/start", -32000, "thread not found: old-thread")
        fake.script_response("thread/start", {"threadId": "new-thread"})
        fake.script_response("turn/start", {"turn": {"id": "t1"}})
        fake.script_notification("turn/completed", {})

        result = await session.run_turn("hello")
        assert result.error is None
        assert result.should_retire is False
        assert session.thread_id == "new-thread"

    @pytest.mark.asyncio
    async def test_fresh_thread_missing_is_not_recovered(
        self, session_with_fake_client,
    ) -> None:
        # Not resuming (no stored thread): a thread-missing error on a
        # just-created thread is a real failure, not a stale-resume — it
        # must surface rather than loop into another thread/start.
        session, fake = session_with_fake_client()
        fake.script_response("thread/start", {"threadId": "t-new"})
        fake.script_error("turn/start", -32000, "thread not found: t-new")

        result = await session.run_turn("hello")
        assert result.error is not None


def test_is_thread_missing_error_classifier():
    from flowly.codex.app_server import CodexRPCError
    from flowly.codex.session import _is_thread_missing_error

    assert _is_thread_missing_error(CodexRPCError(-32000, "thread not found: abc"))
    assert _is_thread_missing_error(CodexRPCError(-32000, "unknown thread"))
    assert not _is_thread_missing_error(CodexRPCError(-32000, "internal error"))
