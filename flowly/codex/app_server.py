"""JSON-RPC 2.0 stdio transport for ``codex app-server``.

The ``codex`` CLI ships a ``codex app-server`` subcommand that exposes
the full Codex agent over a JSON-RPC-over-stdio protocol. This module
is the wire-level speaker — spawn the subprocess, run the
``initialize`` handshake, then trade requests / responses /
notifications back and forth.

Design notes
~~~~~~~~~~~~

* **Asyncio-native.** Flowly's whole agent loop is asyncio; mixing a
  blocking subprocess client in would force every caller to bounce
  through executors. We use ``asyncio.subprocess.create_subprocess_exec``
  and two background reader tasks (stdout + stderr) feeding asyncio
  primitives.

* **Three message classes.** The reader loop classifies every line
  out of stdout into exactly one of:

    1. **Response** — has ``id`` plus ``result`` or ``error``.
       Resolves a future the caller is awaiting.
    2. **Server-initiated request** — has ``id`` plus ``method``.
       Codex asks the client to do something (approval prompts,
       elicitations). Goes on ``_server_request_queue``; the caller
       drains it via ``take_server_request()`` and replies with
       ``respond()`` / ``respond_error()``.
    3. **Notification** — has ``method`` but no ``id``. Item-stream
       events (``item/started``, ``item/<type>/delta``,
       ``item/completed``, ``turn/completed``). Goes on
       ``_notification_queue``; caller drains via
       ``take_notification()``.

* **Stderr is diagnostic.** Codex writes operational logs +
  OAuth-failure hints to stderr. We keep a small ring buffer
  (``_stderr_buffer``) so the session layer can scan it when
  classifying failures.

* **JSON-RPC dialect.** The Codex flavor is JSON-RPC 2.0 with the
  ``jsonrpc: "2.0"`` envelope field **omitted** on the wire — Codex's
  parser is lenient about its presence but the reference clients
  ship without it. We follow the same convention on outgoing
  messages so the wire trace is identical.

* **Lifecycle.** ``spawn()`` is the only constructor; it boots the
  subprocess, starts the readers, runs ``initialize``, and hands
  back a usable client. ``close()`` is the only teardown; it
  cancels readers, drains pending futures, terminates the
  subprocess, and finally kill -9s if needed.

* **Never raise inside readers.** Reader tasks swallow exceptions
  (other than CancelledError) so a malformed line from a buggy
  upstream version doesn't take the whole agent down. Caller-side
  failures surface through ``request()`` / ``take_*()`` instead.

The actual Codex item type catalog (``message``, ``reasoning``,
``commandExecution``, ``fileChange``, ``mcpToolCall``, …) is
projection-layer concern; the transport is item-agnostic and just
hands raw notification dicts upward.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


# Common locations a `codex` install lands in but that aren't always on a
# service's PATH. macOS launchd agents in particular start with a minimal
# PATH (``/usr/bin:/bin:/usr/sbin:/sbin``) that excludes Homebrew
# (``/opt/homebrew/bin``) and npm-global bins, so a `codex` that works in
# the user's interactive shell is invisible to the gateway. We augment the
# PATH search with these so spawning works regardless of how the gateway
# was launched.
# StreamReader buffer ceiling for codex's stdout/stderr. asyncio defaults to
# 64 KiB per line, but codex sends one JSON-RPC message per line and large
# ones (plugin/list, big tool results, file contents) blow past that.
_STREAM_LIMIT = 16 * 1024 * 1024

_CODEX_FALLBACK_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
    os.path.expanduser("~/.nvm/current/bin"),
)


def _resolve_codex_bin(codex_bin: str) -> str:
    """Resolve ``codex_bin`` to an absolute path when possible.

    An explicit path (containing a separator) is returned unchanged. A bare
    name is looked up on PATH augmented with :data:`_CODEX_FALLBACK_DIRS`, so
    a Homebrew/npm install is found even when the gateway runs with a minimal
    PATH (e.g. under launchd). Falls back to the original name when nothing
    matches — the spawn then raises a clear "not found" error.
    """
    if os.sep in codex_bin or (os.altsep and os.altsep in codex_bin):
        return codex_bin
    augmented = os.pathsep.join(
        [os.environ.get("PATH", "")] + list(_CODEX_FALLBACK_DIRS)
    )
    return shutil.which(codex_bin, path=augmented) or codex_bin


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CodexSpawnError(RuntimeError):
    """Raised when the ``codex app-server`` subprocess can't be started.

    Wraps the underlying ``FileNotFoundError`` / ``OSError`` with a
    user-readable message that surfaces the binary path we tried so
    operators can see the actual lookup failure.
    """


class CodexRPCError(RuntimeError):
    """Raised when a JSON-RPC request returns an ``error`` payload.

    Carries the Codex error code (signed int, JSON-RPC convention) and
    optional ``data`` field — the caller surfaces both so the agent
    can decide whether the failure is auth, schema, or transport.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"codex JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class CodexProtocolError(RuntimeError):
    """Raised when Codex sends a message that violates the protocol.

    Examples: missing ``id`` on a response, malformed JSON line,
    unknown top-level shape. Reader logs and continues; only surfaced
    to the caller when a pending future is involved.
    """


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# How many stderr lines to retain for post-hoc diagnostic. Codex
# sometimes prints a multi-line OAuth-failure hint; 256 is generous
# enough to catch it without unbounded memory growth on a long-lived
# session (Codex itself logs verbosely in debug builds).
_STDERR_BUFFER_LINES = 256

# Initialize handshake timeout. The official Codex CLI completes
# initialize in <100ms locally; we cap at 10s to surface obvious
# breakage (wrong binary, missing auth) instead of hanging.
_INITIALIZE_TIMEOUT_S = 10.0

# Graceful shutdown grace before SIGKILL. 5s mirrors Codex's own
# self-shutdown SLO; longer just delays the inevitable on a wedged
# subprocess.
_CLOSE_GRACE_S = 5.0


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class CodexAppServerClient:
    """Minimal asyncio JSON-RPC client for ``codex app-server``.

    Spawn one client per Codex thread you intend to drive. Multiple
    threads can live behind one client (Codex multiplexes them) but
    the simpler 1:1 mapping (one Flowly session ↔ one Codex client ↔
    one Codex thread) avoids cross-thread state leakage and matches
    how the session layer above this class is designed.
    """

    def __init__(self) -> None:
        # Constructor is intentionally minimal — the real wiring lives
        # in ``spawn()`` because subprocess + reader-task startup are
        # async-only. Calling ``CodexAppServerClient()`` directly
        # leaves you with an unusable object; the typing module
        # documents this with ``classmethod spawn() -> Self``.
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        # Outbound request bookkeeping. ``_next_id`` is a strictly
        # monotonic counter so we never reuse an id within a session;
        # Codex correlates strictly on id, so a reused id would route
        # a stale response to a fresh awaiter.
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # ``_lock`` is paranoia: we're single-threaded under asyncio,
        # but acquiring it around _next_id increments + future
        # registration keeps a future re-entrant caller from racing
        # itself (e.g. a hook firing during a request).
        self._lock = asyncio.Lock()

        # Inbound queues. ``_notification_queue`` carries
        # server→client notifications (``item/*``, ``turn/completed``,
        # ``thread/*`` updates). ``_server_request_queue`` carries
        # server-initiated requests that the client must reply to
        # (approval prompts, elicitations). Both are unbounded — the
        # session layer keeps them drained at every loop iteration so
        # backpressure isn't normally a concern.
        self._notification_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._server_request_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Diagnostic state.
        self._stderr_buffer: deque[str] = deque(maxlen=_STDERR_BUFFER_LINES)
        self._closed: bool = False
        # When the subprocess exits unexpectedly, the reader task
        # surfaces the exit code here. ``request()`` checks this on
        # every call so a caller doesn't await a future that will
        # never resolve.
        self._exit_code: int | None = None

    # ── Spawn ────────────────────────────────────────────────────────

    @classmethod
    async def spawn(
        cls,
        *,
        codex_bin: str = "codex",
        codex_home: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        client_name: str = "flowly",
        client_version: str = "1.9.9",
    ) -> "CodexAppServerClient":
        """Spawn ``codex app-server`` and complete the initialize handshake.

        Args:
            codex_bin: Executable name or absolute path. Defaults to
                ``"codex"`` (looked up on PATH); pass an absolute path
                when bundling.
            codex_home: Override for Codex's config dir. Maps to
                ``CODEX_HOME`` env var — controls where Codex reads
                ``auth.json`` and writes thread state. ``None`` →
                Codex's default (``~/.codex``).
            cwd: Working directory the subprocess starts in. Codex
                uses this as the implicit root for ``exec`` /
                ``apply_patch`` operations unless the caller overrides
                it per-thread.
            env: Extra environment variables, merged on top of
                ``os.environ``. Use this to inject ``OPENAI_API_KEY``
                for API-key-based auth or ``no_proxy`` for VPN edge
                cases.
            client_name / client_version: Sent in the ``initialize``
                payload so Codex's diagnostics show ``flowly@1.9.9``
                instead of an anonymous JSON-RPC client.

        Returns:
            A fully-initialized client. The handshake has completed
            and reader tasks are running by the time this returns.

        Raises:
            CodexSpawnError: subprocess failed to start (binary
                missing, permission denied, etc.).
            CodexRPCError: initialize handshake returned an error
                payload — almost always missing/expired auth.
            asyncio.TimeoutError: handshake didn't complete within
                :data:`_INITIALIZE_TIMEOUT_S`.
        """
        self = cls()

        # Build subprocess env: caller's env wins over os.environ,
        # codex_home (if supplied) is folded in as a Codex-specific
        # override. Doing it in two steps (rather than one dict
        # merge) makes the precedence explicit in code review.
        spawn_env = os.environ.copy()
        if env:
            spawn_env.update(env)
        if codex_home:
            spawn_env["CODEX_HOME"] = codex_home
        # The `codex` binary is itself a Node launcher and shells out to
        # `node` (and to the user's build tools) during a turn. If the
        # gateway runs with a minimal PATH (launchd agents do), those
        # children fail with `env: node: No such file or directory` even
        # though codex spawned. Fold the common tool dirs into PATH so
        # codex's whole process tree can find its dependencies.
        _extra_path = [d for d in _CODEX_FALLBACK_DIRS if os.path.isdir(d)]
        if _extra_path:
            _cur = spawn_env.get("PATH", "")
            spawn_env["PATH"] = (
                os.pathsep.join([_cur, *_extra_path]) if _cur
                else os.pathsep.join(_extra_path)
            )
        # Codex emits Rust tracing to stderr at INFO by default,
        # which is noisy enough to drown out the real error hints
        # we want to surface. Set WARN as the floor unless the
        # caller has set RUST_LOG explicitly (debug investigations
        # routinely set RUST_LOG=debug from the shell).
        spawn_env.setdefault("RUST_LOG", "warn")

        # Resolve to an absolute path so the spawn doesn't depend on the
        # gateway's PATH (launchd agents start with a minimal PATH that
        # excludes Homebrew / npm-global — see _resolve_codex_bin).
        resolved_bin = _resolve_codex_bin(codex_bin)

        # A non-existent cwd makes create_subprocess_exec raise the SAME
        # FileNotFoundError as a missing binary — which used to surface as
        # a bogus "codex binary not found" when the model passed "~/project"
        # (the OS never expands the tilde). Expand and verify here so the
        # error names the actual problem.
        if cwd:
            cwd = os.path.expanduser(cwd)
            if not os.path.isdir(cwd):
                raise CodexSpawnError(
                    f"codex working directory does not exist: {cwd!r} — "
                    "pass an existing absolute path (or omit cwd)."
                )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                resolved_bin,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=spawn_env,
                # Raise the StreamReader line limit far above asyncio's 64 KiB
                # default. Codex emits a whole JSON-RPC message per line, and a
                # single one (a large plugin/list response, a big tool result,
                # or a file's content in an item event) routinely exceeds 64 KiB
                # — at which point readline() raises "Separator is not found,
                # and chunk exceed the limit" and the turn dies. 16 MiB covers
                # any realistic codex message.
                limit=_STREAM_LIMIT,
            )
        except FileNotFoundError as exc:
            raise CodexSpawnError(
                f"codex binary not found (looked for {codex_bin!r} on PATH "
                f"+ {', '.join(_CODEX_FALLBACK_DIRS)}). Install via "
                "`npm i -g @openai/codex`, or set an absolute path in "
                "config.json under tools.codexSession.codexBin."
            ) from exc
        except OSError as exc:
            raise CodexSpawnError(
                f"failed to spawn `{codex_bin} app-server`: {exc}"
            ) from exc

        # Start readers BEFORE the handshake — the handshake itself
        # involves an exchange of messages that the reader has to
        # parse, so the loop needs to be running first.
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="codex-app-server-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_reader_loop(), name="codex-app-server-stderr-reader"
        )

        # initialize handshake — non-negotiable; Codex rejects every
        # other method until it succeeds. Two parts to the handshake:
        #
        #   1. ``initialize`` request — exchanges client/server info
        #      and capability dicts. Codex 2026-05+ requires the
        #      ``capabilities`` field even if empty; older builds
        #      tolerated its omission but the live binary silently
        #      stalls if it's missing.
        #
        #   2. ``initialized`` notification — LSP/MCP-style "I've
        #      processed the initialize response; you may now send
        #      server-initiated requests and start streaming". WITHOUT
        #      THIS Codex stays in pre-initialized state, accepts
        #      thread/start (because that's a client-initiated
        #      request), creates the thread, but never streams item/*
        #      notifications back. The turn appears to "hang" forever.
        #      This is the #1 cause of "Codex never responds" bugs in
        #      third-party integrations.
        try:
            await asyncio.wait_for(
                self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": client_name,
                            "title": client_name.title(),
                            "version": client_version,
                        },
                        "capabilities": {},
                    },
                ),
                timeout=_INITIALIZE_TIMEOUT_S,
            )
            # Critical second half of the handshake.
            await self.notify("initialized")
        except Exception:
            # If handshake failed, we own the subprocess — clean up
            # so the caller doesn't get a half-alive client.
            await self.close()
            raise

        return self

    # ── Reader tasks ─────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Drain stdout, classify each line, dispatch.

        Runs until stdout EOFs (subprocess exited) or the task is
        cancelled. Every other failure mode (JSON parse error, missing
        ``id``, unknown shape) is logged and skipped — losing one
        message is preferable to crashing the reader and leaving every
        pending future hanging.
        """
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        try:
            while True:
                line = await stdout.readline()
                if not line:
                    # EOF. Subprocess has exited (or stdout was
                    # closed). Cancel pending futures so callers
                    # awaiting them see a clean failure instead of
                    # hanging forever.
                    await self._on_subprocess_exit()
                    return

                try:
                    msg = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "[codex] dropped malformed stdout line: %s (%s)",
                        line[:200], exc,
                    )
                    continue

                if not isinstance(msg, dict):
                    logger.warning(
                        "[codex] dropped non-object message: %r", msg,
                    )
                    continue

                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[codex] reader loop crashed; pending futures will fail")
            await self._on_subprocess_exit()

    async def _stderr_reader_loop(self) -> None:
        """Drain stderr into the diagnostic ring buffer.

        Codex prints OAuth refresh failure hints, deprecation
        warnings, and verbose debug logs (when ``--verbose`` is set)
        to stderr. The session layer scans this buffer when
        classifying request failures — a generic "server error" with
        ``invalid_grant`` in stderr is almost always auth needing
        refresh.
        """
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr

        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                try:
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    continue
                if text:
                    self._stderr_buffer.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Stderr reader failing is non-fatal — diagnostic stops
            # but the protocol keeps working. Log and exit.
            logger.exception("[codex] stderr reader crashed (non-fatal)")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route one stdout message to the right destination.

        Three valid shapes per JSON-RPC 2.0; we test in **most-
        specific first** order so a response with extra metadata
        can't be misrouted as a server-initiated request:

          * Response: ``{id, result}`` or ``{id, error}``
            → resolve the matching pending future.
          * Server-initiated request: ``{id, method, params?}``
            → enqueue on ``_server_request_queue``.
          * Notification: ``{method, params?}`` (no id)
            → enqueue on ``_notification_queue``.

        Anything else is logged and dropped. The ``jsonrpc: "2.0"``
        envelope key is tolerated but not required (see module
        docstring).
        """
        # 1. Response (id + result/error) — most specific shape. Test
        # first so a malformed message carrying ``method`` alongside
        # ``result`` (rare but observed in some Codex versions)
        # doesn't get misrouted as a server-initiated request.
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut is None or fut.done():
                logger.warning(
                    "[codex] response for unknown / cancelled request id=%r",
                    req_id,
                )
                return

            if "error" in msg:
                err = msg["error"] or {}
                code = err.get("code", -32000)
                message = err.get("message", "unknown error")
                data = err.get("data")
                fut.set_exception(CodexRPCError(code, message, data))
            elif "result" in msg:
                fut.set_result(msg["result"])
            return

        # 2. Server-initiated request (id + method, no result).
        if "id" in msg and "method" in msg:
            await self._server_request_queue.put(msg)
            return

        # 3. Notification (method only, no id).
        if "method" in msg:
            await self._notification_queue.put(msg)
            return

        # 4. Unknown shape — log and drop. Could be a bare ack with
        # only an id (some Codex versions send these on disconnect)
        # or future protocol additions.
        if "id" in msg:
            logger.debug(
                "[codex] received bare id message id=%r — likely an ack",
                msg.get("id"),
            )
            return

        logger.warning("[codex] dropped message with no id/method: %r", msg)

    async def _on_subprocess_exit(self) -> None:
        """Clean up state when the subprocess has terminated.

        Reads the exit code (if available), records it, and cancels
        every pending future so awaiting callers fail fast.
        """
        if self._proc is not None:
            try:
                self._exit_code = self._proc.returncode
            except Exception:
                self._exit_code = None
        self._closed = True

        pending = list(self._pending.items())
        self._pending.clear()
        for req_id, fut in pending:
            if not fut.done():
                stderr_tail = "\n".join(list(self._stderr_buffer)[-10:])
                fut.set_exception(
                    CodexProtocolError(
                        f"codex app-server exited (code={self._exit_code}) "
                        f"with pending request id={req_id}. "
                        f"stderr tail:\n{stderr_tail}"
                    )
                )

    # ── Outbound API ─────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request and await the matching response.

        Args:
            method: JSON-RPC method name. Codex uses dotted paths
                (``thread/start``, ``turn/start``, ``thread/list``).
            params: Optional parameter dict. JSON-serialisable.
            timeout: Wall-clock timeout in seconds. ``None`` waits
                forever; pass an explicit value for any request the
                caller has a deadline budget for. The transport
                doesn't impose a default because reasonable timeouts
                vary enormously by method (initialize: 10s, turn/start
                on a long agentic run: 10 minutes).

        Returns:
            Whatever Codex put under the ``result`` key — Codex's
            schema dictates the shape per method.

        Raises:
            CodexRPCError: Codex returned an ``error`` payload.
            CodexProtocolError: subprocess died mid-flight.
            asyncio.TimeoutError: ``timeout`` was set and elapsed.
            RuntimeError: the client is already closed.
        """
        if self._closed:
            raise RuntimeError(
                "codex client is closed; spawn a new one to send another request"
            )

        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[req_id] = fut

        payload: dict[str, Any] = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        await self._write_line(payload)

        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # Pull the future out of pending so a late response from
            # Codex doesn't try to set a result on a dead future
            # (that would just be a noisy warning, but cleaner this
            # way).
            self._pending.pop(req_id, None)
            raise

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Fire a notification (no response expected, no id).

        Used for things like ``$/cancelRequest`` where the client just
        tells the server something and moves on.
        """
        if self._closed:
            raise RuntimeError("codex client is closed")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(payload)

    async def respond(self, request_id: int, result: Any) -> None:
        """Reply to a server-initiated request with a result.

        Pulled from ``_server_request_queue`` items. Codex matches the
        ``id`` field on response.
        """
        if self._closed:
            raise RuntimeError("codex client is closed")
        await self._write_line({"id": request_id, "result": result})

    async def respond_error(
        self,
        request_id: int,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        """Reply to a server-initiated request with an error.

        Use this for approval prompts when the user declined or the
        client can't fulfil the request (e.g. tool not available).
        """
        if self._closed:
            raise RuntimeError("codex client is closed")
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write_line({"id": request_id, "error": error})

    async def _write_line(self, payload: dict[str, Any]) -> None:
        """Serialise + write one JSON line to stdin.

        Wraps the I/O in a try/except so a closed stdin (subprocess
        exited mid-write) raises something the caller can catch as a
        clean ``CodexProtocolError`` instead of an opaque
        ``BrokenPipeError``.
        """
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._on_subprocess_exit()
            raise CodexProtocolError(
                f"codex stdin closed while writing {payload.get('method')!r}: {exc}"
            ) from exc

    # ── Inbound polling ──────────────────────────────────────────────

    async def take_notification(
        self, timeout: float = 0.0,
    ) -> dict[str, Any] | None:
        """Poll the notification queue.

        Args:
            timeout: How long to wait for the next notification. ``0``
                returns immediately (None if queue is empty); positive
                value blocks until a notification arrives or the
                deadline elapses; pass a large value if you want to
                effectively block.

        Returns:
            The raw notification dict (``{method, params}``) or
            ``None`` on timeout. Session layer projects the dict
            through ``CodexEventProjector`` to get a Flowly message.
        """
        if timeout <= 0:
            try:
                return self._notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(
                self._notification_queue.get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None

    async def take_server_request(
        self, timeout: float = 0.0,
    ) -> dict[str, Any] | None:
        """Poll the server-initiated request queue.

        Symmetric to ``take_notification``; same timeout semantics.
        The returned dict has ``id`` + ``method`` + ``params``; the
        session layer must reply via ``respond()`` or
        ``respond_error()`` or Codex will block on that turn forever.
        """
        if timeout <= 0:
            try:
                return self._server_request_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(
                self._server_request_queue.get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None

    # ── Diagnostics ──────────────────────────────────────────────────

    def is_alive(self) -> bool:
        """Return True if the subprocess is still running and we're
        not in the middle of shutting down."""
        if self._closed:
            return False
        if self._proc is None:
            return False
        return self._proc.returncode is None

    def stderr_tail(self, n: int = 50) -> list[str]:
        """Return the last ``n`` lines of stderr.

        Used by the session layer when classifying request failures
        — Codex's stderr is where OAuth refresh failures, deprecation
        notices, and verbose debug logs surface. Returns a fresh list
        copy so the caller can't accidentally mutate the ring buffer.
        """
        tail = list(self._stderr_buffer)
        if n <= 0 or n >= len(tail):
            return tail
        return tail[-n:]

    @property
    def exit_code(self) -> int | None:
        """Subprocess exit code if it has terminated, else ``None``."""
        return self._exit_code

    # ── Shutdown ─────────────────────────────────────────────────────

    async def close(self, *, grace: float = _CLOSE_GRACE_S) -> int | None:
        """Terminate the subprocess and stop reader tasks.

        Best-effort: tries SIGTERM first, falls back to SIGKILL if the
        subprocess doesn't exit within ``grace`` seconds. Always
        cancels reader tasks and clears pending futures so a botched
        shutdown can't leak resources.

        Returns:
            The subprocess exit code, or ``None`` if the subprocess
            had never been spawned.
        """
        if self._closed and self._proc is None:
            return self._exit_code

        self._closed = True

        # Cancel readers first — they hold references to the streams
        # we're about to close. Letting them race with the
        # terminate() below sometimes produces a ResourceWarning we
        # don't need to chase.
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Terminate subprocess.
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                # Already dead — fine.
                pass

            try:
                await asyncio.wait_for(self._proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                logger.warning(
                    "[codex] subprocess didn't exit on SIGTERM, sending SIGKILL"
                )
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await self._proc.wait()
                except Exception:
                    pass

            try:
                self._exit_code = self._proc.returncode
            except Exception:
                self._exit_code = None

        # Cancel any still-pending futures (close() racing with
        # outstanding requests is the worst case here).
        await self._on_subprocess_exit()

        return self._exit_code

    # ── Context manager sugar ────────────────────────────────────────

    async def __aenter__(self) -> "CodexAppServerClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
