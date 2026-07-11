"""Async WebSocket client for the flowly gateway."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp

from flowly.artifacts.summary import artifact_summary


class GatewayUnavailable(Exception):
    pass


@dataclass
class StreamDelta:
    run_id: str
    text: str


@dataclass
class ChatFinal:
    run_id: str
    session_key: str
    text: str
    usage: dict[str, Any] | None = None


@dataclass
class ChatAborted:
    run_id: str
    session_key: str


@dataclass
class ChatError:
    run_id: str
    session_key: str
    message: str


@dataclass
class ApprovalRequest:
    request_id: str
    command: str
    reasons: list[str]
    raw: dict[str, Any]
    session_key: str = ""
    expires_at: float | None = None
    cwd: str | None = None
    resolved_path: str | None = None
    # Whether "Always allow" is meaningful for this request (default True for
    # back-compat with older gateways that don't send the flag).
    supports_always: bool = True


@dataclass
class ToolStart:
    tool_call_id: str
    name: str
    args: dict[str, Any]
    session_key: str


@dataclass
class ToolComplete:
    tool_call_id: str
    name: str
    success: bool
    duration_ms: int
    preview: str
    session_key: str


@dataclass
class ConnectionLost:
    reason: str


@dataclass
class Reconnecting:
    attempt: int
    delay_s: float
    last_error: str


@dataclass
class Reconnected:
    attempt: int


@dataclass
class SubagentStarted:
    run_id: str
    label: str
    task: str
    model: str
    raw: dict[str, Any]


@dataclass
class SubagentCompleted:
    run_id: str
    label: str
    status: str
    error: str | None
    raw: dict[str, Any]


@dataclass
class CompactionEvent:
    """Gateway broadcasts this when context auto-compacts mid-conversation."""
    before_messages: int
    after_messages: int
    before_tokens: int
    after_tokens: int
    raw: dict[str, Any]


@dataclass
class ArtifactEvent:
    action: str
    artifact: dict[str, Any]


Event = (
    StreamDelta
    | ChatFinal
    | ChatAborted
    | ChatError
    | ApprovalRequest
    | ToolStart
    | ToolComplete
    | ConnectionLost
    | Reconnecting
    | Reconnected
    | SubagentStarted
    | SubagentCompleted
    | CompactionEvent
    | ArtifactEvent
    | dict[str, Any]
)


class GatewayClient:
    """Minimal async client speaking flowly's gateway WS protocol.

    Single connection, single session. The TUI owns one instance.
    Events flow through ``events()`` as a typed async iterator;
    RPCs are fire-and-forget plus optional ``await_reply``.
    """

    # Reconnect tuning — exponential backoff capped at 30s, give up after N
    RECONNECT_BACKOFF: tuple[float, ...] = (1, 2, 4, 8, 15, 30, 30, 30)
    HEARTBEAT_SILENCE_S: float = 30.0   # send manual ping after this much silence
    HEARTBEAT_PONG_TIMEOUT_S: float = 8.0  # treat as dead if no pong arrives

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18790,
        *,
        token: str = "",
        url_provider: "Callable[[], Awaitable[str]] | None" = None,
    ) -> None:
        """Create a gateway client.

        ``host`` / ``port`` produce the default ``ws://host:port/ws`` URL.
        ``token`` is the gateway's remote-access token (``config.gateway.token``):
        when the gateway is exposed remotely it requires auth for EVERY client,
        including this same-machine TUI, so we present the configured token as a
        ``?token=`` query param. Empty token → no param → unchanged loopback
        behaviour (a gateway with no token needs none). By design,
        ``auth_required`` follows the bind mode and local clients still
        authenticate with the configured token (no loopback bypass).
        ``url_provider`` overrides the URL with an awaitable supplier called on
        every (re)connect — used by the relay client to mint a fresh JWT.
        """
        _tok = (token or "").strip()
        self._url = f"ws://{host}:{port}/ws" + (f"?token={quote(_tok, safe='')}" if _tok else "")
        self._url_provider = url_provider
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._inbox: asyncio.Queue[Event] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_recv_ts: float = 0.0
        self._pong_pending: bool = False

    async def _resolve_url(self) -> str:
        """Return the URL to dial. Re-evaluated on every reconnect."""
        if self._url_provider is not None:
            return await self._url_provider()
        return self._url

    async def connect(self) -> None:
        """Initial connect — raises GatewayUnavailable on failure.

        Once connected, ``_supervisor_task`` keeps the connection alive
        across drops (auto-reconnect with backoff). Call ``close()`` to
        stop the supervisor and tear down sockets.
        """
        await self._connect_socket()
        self._supervisor_task = asyncio.create_task(self._supervise())

    async def _connect_socket(self) -> None:
        """Open a single fresh WS — used by both initial connect and reconnect."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = await self._resolve_url()
        try:
            self._ws = await self._session.ws_connect(
                url, heartbeat=20.0, autoping=True
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # Strip query string from logged URL so we don't leak short-lived
            # JWTs into stderr or error banners.
            safe = url.split("?", 1)[0]
            raise GatewayUnavailable(
                f"could not reach gateway at {safe}: {exc}"
            ) from exc
        self._last_recv_ts = time.monotonic()
        self._pong_pending = False
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._watchdog_task = asyncio.create_task(self._heartbeat_watchdog())

    async def _supervise(self) -> None:
        """Keep reconnecting after drops with rapid-drop give-up.

        If 3 freshly-connected sockets close within ``RAPID_DROP_S``
        seconds in a row, we declare the link broken and emit
        ``ConnectionLost``. Without this the transcript fills with
        "reconnected · attempt 1" spam every second when the peer
        rejects every handshake (e.g. relay sees a bad JWT).
        """
        RAPID_DROP_S = 10.0
        RAPID_DROP_GIVE_UP = 3
        consecutive_rapid_drops = 0

        while not self._closed:
            connect_ts = self._last_recv_ts
            if self._reader_task:
                try:
                    await self._reader_task
                except Exception:
                    pass
            if self._closed:
                return

            if connect_ts and (time.monotonic() - connect_ts) < RAPID_DROP_S:
                consecutive_rapid_drops += 1
            else:
                consecutive_rapid_drops = 0

            # Bail out instead of churning forever on a peer that's actively
            # refusing us. The TUI's ConnectionLost handler surfaces the
            # underlying close reason (see ``_last_close_reason``).
            if consecutive_rapid_drops >= RAPID_DROP_GIVE_UP:
                last_reason = getattr(self, "_last_close_reason", "unknown")
                await self._inbox.put(
                    ConnectionLost(
                        reason=(
                            f"peer rejected {consecutive_rapid_drops} attempts in a row · "
                            f"last: {last_reason}"
                        )
                    )
                )
                return

            last_err = "connection lost"
            for attempt, delay in enumerate(self.RECONNECT_BACKOFF, start=1):
                if self._closed:
                    return
                await self._inbox.put(
                    Reconnecting(attempt=attempt, delay_s=delay, last_error=last_err)
                )
                await asyncio.sleep(delay)
                try:
                    if self._watchdog_task and not self._watchdog_task.done():
                        self._watchdog_task.cancel()
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    await self._connect_socket()
                    await self._inbox.put(Reconnected(attempt=attempt))
                    break
                except GatewayUnavailable as exc:
                    last_err = str(exc)
                    continue
            else:
                await self._inbox.put(
                    ConnectionLost(reason=f"gave up reconnecting: {last_err}")
                )
                return

    async def _heartbeat_watchdog(self) -> None:
        """Detect dead connections silently: ping after silence, reconnect on miss."""
        try:
            while not self._closed and self._ws and not self._ws.closed:
                await asyncio.sleep(2.0)
                silence = time.monotonic() - self._last_recv_ts
                if silence < self.HEARTBEAT_SILENCE_S:
                    continue
                if self._pong_pending:
                    if silence > self.HEARTBEAT_SILENCE_S + self.HEARTBEAT_PONG_TIMEOUT_S:
                        # No pong despite our nudge — force the reader to exit.
                        if self._ws and not self._ws.closed:
                            await self._ws.close(code=4000, message=b"watchdog timeout")
                        return
                    continue
                # Send a manual JSON ping that the server explicitly handles.
                self._pong_pending = True
                try:
                    if self._ws and not self._ws.closed:
                        await self._ws.send_json({
                            "type": "ping",
                            "timestamp": int(time.monotonic() * 1000),
                        })
                except Exception:
                    return
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self._closed = True
        for t in (self._supervisor_task, self._watchdog_task, self._reader_task):
            if t and not t.done():
                t.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # --- public RPC helpers ----------------------------------------

    async def chat_send(
        self,
        message: str,
        *,
        session_key: str,
        run_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "message": message,
            "sessionKey": session_key,
            "idempotencyKey": run_id or str(uuid.uuid4()),
        }
        if attachments:
            params["attachments"] = attachments
        rid = await self._rpc(
            "chat.send",
            params,
        )
        reply = await self._await_reply(rid, timeout=10.0)
        return reply.get("runId", "")

    async def chat_abort(self, run_id: str) -> None:
        await self._rpc("chat.abort", {"runId": run_id})

    async def chat_history(self, session_key: str, limit: int = 50) -> list[dict[str, Any]]:
        rid = await self._rpc("chat.history", {"sessionKey": session_key, "limit": limit})
        reply = await self._await_reply(rid, timeout=10.0)
        return reply.get("messages", [])

    async def chat_compact(
        self, session_key: str, instructions: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sessionKey": session_key}
        if instructions:
            params["instructions"] = instructions
        rid = await self._rpc("chat.compact", params)
        return await self._await_reply(rid, timeout=120.0)

    async def chat_clear(self, session_key: str) -> dict[str, Any]:
        rid = await self._rpc("chat.clear", {"sessionKey": session_key})
        return await self._await_reply(rid, timeout=10.0)

    async def chat_retry(self, session_key: str) -> dict[str, Any]:
        """Drop trailing assistant chain; return ``{ok, text, removed}``.

        Client typically follows up with ``chat_send(text)`` when ``ok``
        and surfaces ``reason`` as a no-op message otherwise.
        """
        rid = await self._rpc("chat.retry", {"sessionKey": session_key})
        return await self._await_reply(rid, timeout=10.0)

    async def chat_undo(self, session_key: str) -> dict[str, Any]:
        """Drop the last user+assistant turn; return ``{ok, text, removed}``.

        Client uses ``text`` to optionally pre-fill the composer for an
        edit-and-resubmit flow, then refreshes via ``chat_history``.
        """
        rid = await self._rpc("chat.undo", {"sessionKey": session_key})
        return await self._await_reply(rid, timeout=10.0)

    async def commands_list(self) -> dict[str, list[dict[str, Any]]]:
        rid = await self._rpc("commands.list", {})
        return await self._await_reply(rid, timeout=10.0)

    async def board_snapshot(self) -> dict[str, Any] | None:
        rid = await self._rpc("board.snapshot", {})
        reply = await self._await_reply(rid, timeout=10.0)
        return reply.get("snapshot")

    async def board_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        rid = await self._rpc("board.action", payload)
        return await self._await_reply(rid, timeout=15.0)

    async def sessions_list(self, limit: int = 50) -> list[dict[str, Any]]:
        rid = await self._rpc("sessions.list", {"limit": limit})
        reply = await self._await_reply(rid, timeout=10.0)
        return reply.get("sessions", [])

    async def session_delete(self, session_key: str) -> bool:
        rid = await self._rpc("sessions.delete", {"sessionKey": session_key})
        reply = await self._await_reply(rid, timeout=10.0)
        return bool(reply.get("deleted"))

    async def assistants_list(self) -> list[dict[str, Any]]:
        rid = await self._rpc("assistants.list", {})
        reply = await self._await_reply(rid, timeout=10.0)
        return reply.get("assistants", [])

    async def audit_list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        tool: str | None = None,
        date: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tool: params["tool"] = tool
        if date: params["date"] = date
        if search: params["search"] = search
        rid = await self._rpc("audit.list", params)
        reply = await self._await_reply(rid, timeout=15.0)
        return reply.get("entries", []) if isinstance(reply, dict) else []

    async def audit_stats(self) -> dict[str, Any]:
        rid = await self._rpc("audit.stats", {})
        return await self._await_reply(rid, timeout=10.0)

    async def approval_list(self) -> list[dict[str, Any]]:
        rid = await self._rpc("exec.approval.list", {})
        reply = await self._await_reply(rid, timeout=5.0)
        return reply.get("approvals", [])

    # --- memory review queue ---------------------------------------

    async def memory_review(self) -> list[dict[str, Any]]:
        """Items awaiting review (the governance ``needs_review`` queue)."""
        rid = await self._rpc("memory.review", {})
        reply = await self._await_reply(rid, timeout=5.0)
        return reply.get("items", [])

    async def memory_accept(self, item_id: str) -> dict[str, Any] | None:
        rid = await self._rpc("memory.accept", {"id": item_id})
        reply = await self._await_reply(rid, timeout=5.0)
        return reply.get("item")

    async def memory_reject(self, item_id: str) -> dict[str, Any] | None:
        rid = await self._rpc("memory.reject", {"id": item_id})
        reply = await self._await_reply(rid, timeout=5.0)
        return reply.get("item")

    async def artifacts_list(
        self,
        *,
        limit: int = 50,
        search: str | None = None,
        type: str | None = None,
        session_key: str | None = None,
        include_content: bool = True,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit,
            "includeContent": include_content,
        }
        if search:
            params["search"] = search
        if type:
            params["type"] = type
        if session_key is not None:
            params["sessionKey"] = session_key
        rid = await self._rpc("artifacts.list", params)
        reply = await self._await_reply(rid, timeout=10.0)
        artifacts = list(reply.get("artifacts", []))
        # Defensive compatibility with older gateways that ignore sessionKey
        # and includeContent. Never leak another chat's rows into the hint.
        if session_key is not None:
            artifacts = [
                artifact
                for artifact in artifacts
                if artifact.get("session_key") == session_key
            ]
        if not include_content:
            artifacts = [artifact_summary(artifact) for artifact in artifacts]
        return artifacts

    async def artifacts_get(self, artifact_id: str) -> dict[str, Any] | None:
        rid = await self._rpc("artifacts.get", {"id": artifact_id})
        reply = await self._await_reply(rid, timeout=10.0)
        return reply.get("artifact") if isinstance(reply, dict) else None

    # --- subagent specialists + manual spawn -----------------------

    async def subagents_assistants(self) -> dict[str, Any]:
        """Specialists + their per-specialist model overrides + the bot model."""
        rid = await self._rpc("subagents.assistants", {})
        return await self._await_reply(rid, timeout=10.0)

    async def subagents_set_model(self, name: str, model: str) -> dict[str, Any]:
        """Set/clear a specialist's model override ('' clears, 'inherit', or id)."""
        rid = await self._rpc("subagents.set_model", {"name": name, "model": model})
        return await self._await_reply(rid, timeout=10.0)

    async def subagents_spawn(
        self, task: str, *, session_key: str, assistant: str | None = None
    ) -> dict[str, Any]:
        """Manually launch a background subagent; result lands in ``session_key``."""
        params: dict[str, Any] = {"task": task, "sessionKey": session_key}
        if assistant:
            params["assistant"] = assistant
        rid = await self._rpc("subagents.spawn", params)
        return await self._await_reply(rid, timeout=15.0)

    async def approval_resolve(
        self, request_id: str, decision: str, *, remember: bool = False
    ) -> None:
        # decision: "allow-once" | "allow-always" | "deny"
        await self._rpc(
            "exec.approval.resolve",
            {"id": request_id, "requestId": request_id, "decision": decision, "remember": remember},
        )

    # --- standing approval policy ----------------------------------

    async def exec_policy_get(self) -> dict[str, Any]:
        """Current standing exec policy: {security, ask, allowlist:[...]}."""
        rid = await self._rpc("exec.policy.get", {})
        return await self._await_reply(rid, timeout=5.0)

    async def exec_policy_set(
        self, *, security: str | None = None, ask: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if security is not None:
            params["security"] = security
        if ask is not None:
            params["ask"] = ask
        rid = await self._rpc("exec.policy.set", params)
        return await self._await_reply(rid, timeout=5.0)

    async def exec_policy_allowlist_remove(self, pattern: str) -> dict[str, Any]:
        rid = await self._rpc("exec.policy.allowlist.remove", {"pattern": pattern})
        return await self._await_reply(rid, timeout=5.0)

    async def codex_policy_get(self) -> dict[str, Any]:
        """Current codex_session policy: {enabled, sandbox, approvalPolicy, ...}."""
        rid = await self._rpc("codex.policy.get", {})
        return await self._await_reply(rid, timeout=5.0)

    async def codex_policy_set(
        self, *, approval_policy: str | None = None, sandbox: str | None = None
    ) -> dict[str, Any]:
        """Set codex approval policy / sandbox. Applied live by the gateway
        (warm-session drop + tool re-register); ``willRestart`` in the reply
        means no live-reload callback was wired so a restart is needed instead.
        A little more headroom than the exec calls: the live reload drops warm
        Codex subprocesses, which can take a moment.
        """
        params: dict[str, Any] = {}
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if sandbox is not None:
            params["sandbox"] = sandbox
        rid = await self._rpc("codex.policy.set", params)
        return await self._await_reply(rid, timeout=10.0)

    # --- event consumption -----------------------------------------

    async def events(self) -> AsyncIterator[Event]:
        while True:
            ev = await self._inbox.get()
            yield ev

    # --- internals -------------------------------------------------

    async def _rpc(self, method: str, params: dict[str, Any]) -> str:
        assert self._ws is not None
        rpc_id = str(uuid.uuid4())
        await self._ws.send_json(
            {"type": "rpc", "id": rpc_id, "method": method, "params": params}
        )
        return rpc_id

    async def _await_reply(self, rpc_id: str, *, timeout: float) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rpc_id, None)

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        reason = "closed by peer"
        try:
            async for msg in self._ws:
                self._last_recv_ts = time.monotonic()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "pong":
                        self._pong_pending = False
                        continue
                    self._pong_pending = False
                    await self._dispatch(data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    # Capture WS close-frame code + reason so we can tell
                    # "auth rejected" (1008) from "server crashed" (1011)
                    # from custom application codes (4xxx).
                    code = self._ws.close_code if self._ws else None
                    extra = ""
                    try:
                        if hasattr(msg, "data") and msg.data:
                            extra = f" data={msg.data!r}"
                    except Exception:
                        pass
                    reason = f"close ({msg.type.name}, code={code}{extra})"
                    self._last_close_code = code
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    reason = f"socket error: {self._ws.exception()}"
                    break
                elif msg.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                    self._pong_pending = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            reason = f"{type(exc).__name__}: {exc}"
        finally:
            self._last_close_reason = reason
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(GatewayUnavailable(reason))
            self._pending.clear()
            # Surface the close reason into the audit log — invaluable
            # for diagnosing the "relay accepts then immediately closes"
            # pattern (look for code=1008 = bad JWT, or code=4xxx app).
            try:
                from flowly.account import audit_log
                audit_log.warn("ws.reader.exit", reason=reason)
            except Exception:
                pass

    async def _dispatch(self, data: dict[str, Any]) -> None:
        mtype = data.get("type")
        if mtype == "rpc":
            rid = data.get("id", "")
            fut = self._pending.get(rid)
            if fut and not fut.done():
                if "result" in data:
                    fut.set_result(data["result"])
                elif "error" in data:
                    fut.set_exception(RuntimeError(str(data["error"])))
            return

        if mtype != "event":
            return

        ev_name = data.get("event")
        payload = data.get("data") or {}

        if ev_name == "agent" and payload.get("stream") == "assistant":
            inner = payload.get("data") or {}
            await self._inbox.put(
                StreamDelta(run_id=payload.get("runId", ""), text=inner.get("text", ""))
            )
            return

        if ev_name == "chat":
            state = payload.get("state")
            run_id = payload.get("runId", "")
            session_key = payload.get("sessionKey", "")
            if state == "final":
                msg = payload.get("message") or {}
                content = msg.get("content") or []
                text = ""
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text += part.get("text", "")
                usage = msg.get("usage") or payload.get("usage")
                await self._inbox.put(
                    ChatFinal(run_id, session_key, text, usage=usage)
                )
            elif state == "aborted":
                await self._inbox.put(ChatAborted(run_id, session_key))
            elif state == "error":
                await self._inbox.put(
                    ChatError(run_id, session_key, payload.get("errorMessage", ""))
                )
            return

        if ev_name == "exec.approval.requested":
            await self._inbox.put(
                ApprovalRequest(
                    request_id=payload.get("requestId") or payload.get("id") or "",
                    command=payload.get("command", ""),
                    reasons=list(
                        payload.get("reasons")
                        or payload.get("riskReasons")
                        or []
                    ),
                    session_key=str(payload.get("sessionKey") or ""),
                    expires_at=payload.get("expiresAt"),
                    cwd=payload.get("cwd"),
                    resolved_path=payload.get("resolvedPath"),
                    supports_always=bool(payload.get("supportsAlways", True)),
                    raw=payload,
                )
            )
            return

        if ev_name == "tool.start":
            await self._inbox.put(
                ToolStart(
                    tool_call_id=str(payload.get("toolCallId", "")),
                    name=str(payload.get("name", "?")),
                    args=dict(payload.get("args") or {}),
                    session_key=str(payload.get("sessionKey", "")),
                )
            )
            return

        if ev_name == "tool.complete":
            await self._inbox.put(
                ToolComplete(
                    tool_call_id=str(payload.get("toolCallId", "")),
                    name=str(payload.get("name", "?")),
                    success=bool(payload.get("success")),
                    duration_ms=int(payload.get("durationMs") or 0),
                    preview=str(payload.get("preview", "")),
                    session_key=str(payload.get("sessionKey", "")),
                )
            )
            return

        if ev_name == "subagent.started":
            await self._inbox.put(
                SubagentStarted(
                    run_id=str(payload.get("runId") or payload.get("run_id") or ""),
                    label=str(payload.get("label", "?")),
                    task=str(payload.get("task", "")),
                    model=str(payload.get("model", "")),
                    raw=payload,
                )
            )
            return

        if ev_name == "subagent.completed":
            await self._inbox.put(
                SubagentCompleted(
                    run_id=str(payload.get("runId") or payload.get("run_id") or ""),
                    label=str(payload.get("label", "?")),
                    status=str(payload.get("status") or payload.get("outcome") or "ok"),
                    error=payload.get("error"),
                    raw=payload,
                )
            )
            return

        if ev_name in ("artifact.created", "artifact.updated", "artifact.deleted"):
            artifact = payload
            if ev_name != "artifact.deleted" and payload.get("id"):
                artifact = artifact_summary(payload)
            await self._inbox.put(
                ArtifactEvent(action=ev_name.removeprefix("artifact."), artifact=artifact)
            )
            return

        if ev_name == "compaction":
            def _g(k1: str, k2: str) -> int:
                v = payload.get(k1) or payload.get(k2) or 0
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
            await self._inbox.put(
                CompactionEvent(
                    before_messages=_g("beforeMessages", "before_messages"),
                    after_messages=_g("afterMessages", "after_messages"),
                    before_tokens=_g("beforeTokens", "before_tokens"),
                    after_tokens=_g("afterTokens", "after_tokens"),
                    raw=payload,
                )
            )
            return

        # unknown event — surface raw so app can log it
        await self._inbox.put({"event": ev_name, "data": payload})
