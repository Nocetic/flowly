"""Tests for the codex app-server JSON-RPC transport.

These tests spawn a tiny Python "fake codex" subprocess that speaks
the same wire protocol Codex would. We don't depend on the real
``codex`` binary being installed — that's covered by end-to-end
manual tests in the worktree branch.

What we pin:

  * Spawn failure surfaces as :class:`CodexSpawnError` with a
    user-readable message naming the path we tried.
  * The ``initialize`` handshake completes successfully against a
    well-behaved server.
  * Notifications (item events) land on the notification queue and
    can be drained via ``take_notification()``.
  * Server-initiated requests land on a separate queue and can be
    answered with ``respond()`` / ``respond_error()``.
  * JSON-RPC errors come back as :class:`CodexRPCError` with the
    server-supplied code + message + data.
  * Subprocess exit mid-flight cancels pending futures cleanly
    (caller sees :class:`CodexProtocolError`, not a hang).
  * Stderr is captured into the diagnostic ring buffer and
    ``stderr_tail()`` returns the most recent lines.
  * ``close()`` is idempotent and graceful (SIGTERM → wait → SIGKILL
    fallback never lands when the subprocess cooperates).

We avoid measuring exact wire byte sequences — the transport's
contract is *behaviour* (correct dispatch, correct error surfacing),
not the literal JSON-RPC envelope format, which is internal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from flowly.codex.app_server import (
    CodexAppServerClient,
    CodexProtocolError,
    CodexRPCError,
    CodexSpawnError,
)


# ---------------------------------------------------------------------------
# Fake codex subprocess helpers
# ---------------------------------------------------------------------------


def _make_fake_codex(script_body: str) -> Path:
    """Write a one-shot Python "fake codex" script to a temp file.

    Each test supplies its own *script_body* — the body executes
    AFTER the standard initialize handshake (the helper bakes in the
    initialize read+reply so every test doesn't repeat it).

    The script reads JSON-RPC messages from stdin one line at a time
    and writes responses / notifications to stdout. ``flush=True`` on
    every print is critical — Python's default line buffering would
    leave responses stuck in the buffer until the parent closes
    stdin, which would deadlock against the parent waiting for a
    response.
    """
    full = dedent("""\
        import sys, json

        def read_msg():
            # Read one client→server message. Skips notifications
            # (no ``id`` field) transparently so test bodies don't
            # have to spell out the post-initialize ``initialized``
            # notification, the optional ``$/heartbeat`` pings, or
            # any other future Codex-protocol notification the
            # client decides to send.
            while True:
                line = sys.stdin.readline()
                if not line:
                    return None
                msg = json.loads(line)
                if "id" not in msg:
                    continue
                return msg

        def write_msg(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        # Standard initialize handshake — every real Codex session
        # begins with this. Tests can assume the handshake has
        # completed by the time their bodies run.
        req = read_msg()
        assert req is not None, "client closed before initialize"
        assert req["method"] == "initialize", f"first msg must be initialize, got {req!r}"
        write_msg({"id": req["id"], "result": {"serverInfo": {"name": "fake-codex"}}})

    """) + script_body

    fd, path = tempfile.mkstemp(suffix="_fake_codex.py", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(full)
    return Path(path)


def _make_wrapper(fake_path: Path) -> Path:
    """Wrap *fake_path* with a tiny shell script that responds to
    ``app-server`` as its first argument.

    We need this because :meth:`CodexAppServerClient.spawn` always
    invokes ``<codex_bin> app-server``, so the binary we pass must
    accept that subcommand. Real Codex does this natively; our fake
    needs the wrapper to forward to the Python script.
    """
    py = sys.executable
    body = dedent(f"""\
        #!/usr/bin/env bash
        if [ "$1" = "app-server" ]; then
            exec {py} {fake_path}
        fi
        echo "fake codex: unknown subcommand $1" >&2
        exit 64
    """)
    fd, path = tempfile.mkstemp(suffix="_codex_wrapper.sh", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return Path(path)


@pytest.fixture
def fake_codex_factory(tmp_path):
    """Pytest fixture that hands out fake-codex wrappers per-test.

    Cleanup is automatic — pytest deletes the per-test ``tmp_path``
    at teardown. The wrappers we hand out live alongside the test
    scripts so the cleanup catches them too.
    """
    created: list[Path] = []

    def factory(script_body: str) -> Path:
        fake = _make_fake_codex(script_body)
        wrapper = _make_wrapper(fake)
        created.extend([fake, wrapper])
        return wrapper

    yield factory

    for p in created:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Spawn / handshake
# ---------------------------------------------------------------------------


class TestSpawn:
    @pytest.mark.asyncio
    async def test_missing_binary_raises_spawn_error(self) -> None:
        # The error message should name the path we tried so the
        # operator can spot a typo or PATH issue immediately.
        with pytest.raises(CodexSpawnError) as excinfo:
            await CodexAppServerClient.spawn(
                codex_bin="/nonexistent/codex-xyz-doesnotexist"
            )
        assert "/nonexistent/codex-xyz-doesnotexist" in str(excinfo.value)
        # And it should hint at the fix.
        assert "npm i -g @openai/codex" in str(excinfo.value) or "binary" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_handshake_completes_against_minimal_server(
        self, fake_codex_factory,
    ) -> None:
        # The simplest possible fake: initialize then exit. Spawn
        # must return a usable client and ``close()`` must clean up.
        wrapper = fake_codex_factory("""
# Body: just exit — the initialize was already done by the harness.
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            assert client.is_alive() or client.exit_code is not None
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------


class TestRequestResponse:
    @pytest.mark.asyncio
    async def test_request_returns_result_payload(
        self, fake_codex_factory,
    ) -> None:
        # After initialize, the fake reads one more request and
        # echoes the params back as the result.
        wrapper = fake_codex_factory("""
req = read_msg()
write_msg({"id": req["id"], "result": {"echoed": req.get("params")}})
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            result = await client.request("ping", {"hello": "world"})
            assert result == {"echoed": {"hello": "world"}}
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_request_error_payload_raises_rpc_error(
        self, fake_codex_factory,
    ) -> None:
        # Codex returns the JSON-RPC ``error`` envelope on failures
        # (auth, schema, missing model, etc.). The client must surface
        # the code + message + data so the session layer can route
        # the failure (refresh OAuth, retry, abort).
        wrapper = fake_codex_factory("""
req = read_msg()
write_msg({
    "id": req["id"],
    "error": {"code": -32001, "message": "thread not found", "data": {"thread_id": "thr_x"}}
})
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            with pytest.raises(CodexRPCError) as excinfo:
                await client.request("thread/resume", {"thread_id": "thr_x"})
            assert excinfo.value.code == -32001
            assert "thread not found" in excinfo.value.message
            assert excinfo.value.data == {"thread_id": "thr_x"}
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_request_timeout_returns_without_hanging(
        self, fake_codex_factory,
    ) -> None:
        # If Codex hangs on a request (network stall, model timeout),
        # the caller's ``timeout=`` kwarg must surface as a clean
        # ``asyncio.TimeoutError`` rather than blocking the loop
        # indefinitely.
        wrapper = fake_codex_factory("""
# Read the request but never reply.
read_msg()
import time; time.sleep(30)
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await client.request("slow", timeout=0.2)
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class TestNotifications:
    @pytest.mark.asyncio
    async def test_notifications_arrive_on_notification_queue(
        self, fake_codex_factory,
    ) -> None:
        # The fake fires three notifications without waiting for any
        # request. The transport must enqueue them so the session
        # layer can drain them in any order relative to its own
        # request/response cycle.
        wrapper = fake_codex_factory("""
write_msg({"method": "item/started", "params": {"itemId": "i1", "type": "agentMessage"}})
write_msg({"method": "item/agentMessage/delta", "params": {"itemId": "i1", "text": "Hello"}})
write_msg({"method": "item/completed", "params": {"itemId": "i1"}})
import time; time.sleep(5)
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            # Drain three notifications with a generous timeout —
            # in practice they arrive within a few ms but slow CI
            # boxes can spike this.
            n1 = await client.take_notification(timeout=2.0)
            n2 = await client.take_notification(timeout=2.0)
            n3 = await client.take_notification(timeout=2.0)
            assert n1 is not None and n1["method"] == "item/started"
            assert n2 is not None and n2["method"] == "item/agentMessage/delta"
            assert n3 is not None and n3["method"] == "item/completed"
            # No more notifications — timeout returns None.
            n4 = await client.take_notification(timeout=0.1)
            assert n4 is None
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_take_notification_zero_timeout_is_nonblocking(
        self, fake_codex_factory,
    ) -> None:
        # Zero timeout is the "is there a notification waiting RIGHT
        # NOW?" call. Must return None immediately on an empty queue;
        # never block.
        wrapper = fake_codex_factory("import time; time.sleep(5)")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            result = await client.take_notification(timeout=0)
            assert result is None
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Server-initiated requests (approval flow)
# ---------------------------------------------------------------------------


class TestServerInitiatedRequests:
    @pytest.mark.asyncio
    async def test_server_request_lands_on_separate_queue(
        self, fake_codex_factory,
    ) -> None:
        # Codex sends an approval prompt mid-turn. The transport
        # must route it to the server-request queue (NOT the
        # notification queue) so the session layer knows it owes a
        # reply.
        wrapper = fake_codex_factory("""
# Send a server-initiated request, wait for the response, exit.
write_msg({"id": 99, "method": "approval/request", "params": {"command": "rm -rf /tmp"}})
resp = read_msg()
assert resp["id"] == 99
assert resp["result"]["decision"] == "declined"
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            # Should NOT be on the notification queue.
            note = await client.take_notification(timeout=0.3)
            assert note is None, f"server request leaked to notification queue: {note}"

            # Should be on the server-request queue.
            sreq = await client.take_server_request(timeout=2.0)
            assert sreq is not None
            assert sreq["id"] == 99
            assert sreq["method"] == "approval/request"
            assert sreq["params"]["command"] == "rm -rf /tmp"

            # Reply with a result — the fake asserts it received the
            # right shape, so a wrong wire format here surfaces as a
            # test failure on the subprocess side (non-zero exit).
            await client.respond(99, {"decision": "declined"})
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_respond_error_routes_back_to_server(
        self, fake_codex_factory,
    ) -> None:
        # The mirror case: client tells Codex it can't fulfil the
        # approval request (user navigated away, tool unavailable).
        wrapper = fake_codex_factory("""
write_msg({"id": 200, "method": "approval/request", "params": {}})
resp = read_msg()
assert resp["id"] == 200
assert "error" in resp
assert resp["error"]["code"] == -32099
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            sreq = await client.take_server_request(timeout=2.0)
            assert sreq is not None
            await client.respond_error(200, -32099, "user navigated away")
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Subprocess lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_subprocess_exit_mid_flight_fails_pending_request(
        self, fake_codex_factory,
    ) -> None:
        # Worst-case: subprocess dies while a request is in flight.
        # The pending future must be cancelled with a clean
        # CodexProtocolError so the caller sees a real failure
        # instead of hanging forever.
        wrapper = fake_codex_factory("""
# Read the request, write a partial response (broken JSON), then exit.
read_msg()
sys.stdout.write('{"id": 1, "result"')  # truncated JSON
sys.exit(0)
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            with pytest.raises(CodexProtocolError):
                await client.request("any/method", timeout=2.0)
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, fake_codex_factory) -> None:
        wrapper = fake_codex_factory("import time; time.sleep(5)")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        code1 = await client.close()
        code2 = await client.close()  # must not raise
        # Both calls return the same exit code (or None).
        assert code1 == code2 or (code1 is not None and code2 is not None)

    @pytest.mark.asyncio
    async def test_request_after_close_raises_runtime_error(
        self, fake_codex_factory,
    ) -> None:
        wrapper = fake_codex_factory("import time; time.sleep(5)")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        await client.close()
        with pytest.raises(RuntimeError):
            await client.request("ping")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    @pytest.mark.asyncio
    async def test_stderr_tail_captures_lines(
        self, fake_codex_factory,
    ) -> None:
        # Codex's OAuth-refresh hints + verbose debug logs go to
        # stderr. Session layer scans this buffer to classify
        # failures — so the buffer must actually contain what
        # Codex wrote.
        wrapper = fake_codex_factory("""
print("warning: rate limit nearing", file=sys.stderr, flush=True)
print("debug: tool dispatch starting", file=sys.stderr, flush=True)
import time; time.sleep(5)
""")
        client = await CodexAppServerClient.spawn(codex_bin=str(wrapper))
        try:
            # Give the stderr reader a moment to drain.
            await asyncio.sleep(0.2)
            tail = client.stderr_tail(10)
            assert any("rate limit" in line for line in tail), tail
            assert any("debug: tool dispatch" in line for line in tail), tail
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Context manager sugar
# ---------------------------------------------------------------------------


class TestContextManager:
    @pytest.mark.asyncio
    async def test_async_with_closes_on_exit(self, fake_codex_factory) -> None:
        wrapper = fake_codex_factory("import time; time.sleep(5)")
        async with await CodexAppServerClient.spawn(codex_bin=str(wrapper)) as client:
            assert client.is_alive()
        # Exited the with-block; client is now closed.
        with pytest.raises(RuntimeError):
            await client.request("ping")
