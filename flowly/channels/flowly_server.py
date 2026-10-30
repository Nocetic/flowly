"""The Flowly server channel: the relay's frames over the server's agent socket.

server/PROTOCOL.md section B. The Flowly server speaks today's relay agent
protocol (rpc / event / ready / conversation.open / media.fetch …), so this is
the relay ``WebChannel`` with two differences:

- authentication: ``Authorization: Bearer <agentSecret>`` and
  ``X-Flowly-Agent-Host: <agentHostId>`` headers instead of a JWT signed with
  the shared relay secret (B1);
- power (B4): ``power.status``, ``power.sleep``, ``power.wake`` and
  ``heartbeat.trigger`` from the server, and ``power.next_due`` pushed to it.

It keeps the channel name ``web``: conversations, cron delivery and the
gateway wiring stay exactly as they are with the relay.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import FLOWLY_SERVER_URL_PREFIXES, FlowlyServerChannelConfig, WebChannelConfig

#: RPCs only the server itself may call; they arrive without a client sessionId.
POWER_METHODS = frozenset({"power.status", "power.sleep", "power.wake", "heartbeat.trigger"})
#: Client RPCs that only read or poll: an open app alone must not keep a cloud
#: machine awake (docs/cloud/ARCHITECTURE.md, the sleep principle).
PASSIVE_METHODS = frozenset({
    "chat.inflight", "chat.history", "chat.command", "commands.list",
    "objectives.list", "agenda.list",
    # A desktop says every 30 s that its owner is at the computer (flowly.push.presence).
    "presence.report",
    # An app reading what secrets are saved or waiting (names only).
    "secrets.requests", "secrets.list",
})

#: What Core asks the server for on connect (server PROTOCOL H1): the relay's
#: voice account identity handshake. A server never offers it to a Core that
#: does not ask, because after it Core drops a client's unwrapped frames.
VOICE_AUTHORITY_FEATURE = "voice-authority-v1"

#: While backing off, look at the clocks this often for a resume.
RESUME_TICK_SECONDS = 0.5
#: The wall clock ran this much further than the monotonic one within a tick:
#: the machine was suspended (or the clock stepped; reconnecting early is harmless).
RESUME_JUMP_SECONDS = 2.0

#: A named agent's chat calls the server sends with ``params.profile`` (server
#: PROTOCOL B7): Core runs them in that agent, in its one conversation.
PROFILE_CHAT_METHODS = frozenset({
    "chat.send", "chat.inflight", "chat.abort", "chat.steer", "chat.command",
    # The server reads the agent's chat from Core only to move it over (B7 "Taşıma").
    "chat.history",
})
#: A named agent's secrets (flowly.secrets), called on the agent's own address
#: the same way: the server sends them with ``params.profile``.
PROFILE_SECRET_METHODS = frozenset({
    "secrets.requests", "secrets.status", "secrets.list", "secrets.submit", "secrets.decline", "secrets.delete",
})
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: The server's delivery session for a named agent's chat: ``srv-p:<profile>:<conversationId>``.
_PROFILE_DELIVERY_SESSION = re.compile(r"^srv-p:([A-Za-z0-9._-]{1,64}):([A-Za-z0-9_-]{1,128})$")
#: A named agent's events the server stores and fans out as its chat (B7).
#: Roster and connection events stay on the ``profiles.rpc`` path.
PROFILE_CHAT_EVENTS = frozenset({
    "chat", "agent", "compaction", "goal.updated", "queue",
    # The agent's reaction on the person's message (server A8, chat-one-log.md §13).
    "chat.reaction",
    "exec.approval.requested", "exec.approval.closed",
    "agent.clarify.requested", "agent.clarify.closed",
    # A secret request's card (flowly.secrets.requests); never a value.
    "secret.requested", "secret.closed",
    "plan.updated", "plan.approval.requested",
})

SLEEPING_ERROR = {
    "code": "AGENT_SLEEPING",
    "message": "The agent is going to sleep. Try again after it wakes.",
    "retryable": True,
}


def _is_passive(method: str) -> bool:
    return method in PASSIVE_METHODS or method.endswith(".status")


def _server_own_call(msg: dict) -> bool:
    """A call the server itself made: no client session, or one of its inbox delivery sessions."""
    session = msg.get("sessionId")
    return session is None or session == "" or (isinstance(session, str) and session.startswith("srv-"))


def _server_screen_rpc_class():
    from flowly.desktop_runtime.relay_rpc import RelayScreenRpc

    class _ServerScreenRpc(RelayScreenRpc):
        """``RelayScreenRpc`` whose ``display.observe`` tickets open this machine's stream directly.

        Over the relay a viewer redeemed its ticket through the relay's stream
        pairing. Here the app connects to ``wss://<FLY_APP_NAME>.fly.dev`` (C), so
        the ticket is also accepted in place of the gateway token on the stream
        route, as ``screen.ticket``'s are, and the reply names that address.
        """

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._methods: dict[tuple[str, str], str] = {}

        async def submit(self, session, message):
            rpc_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(rpc_id, str) and isinstance(message.get("method"), str):
                if len(self._methods) >= 512:
                    self._methods.pop(next(iter(self._methods)))
                self._methods[(session, rpc_id)] = message["method"]
            await super().submit(session, message)

        async def emit(self, session, message, client=None):
            if isinstance(message, dict) and message.get("type") == "rpc":
                method = self._methods.pop((session, message.get("id")), None)
                result = message.get("result")
                if method == "display.observe" and isinstance(result, dict):
                    message = {**message, "result": _direct_stream(self.gateway, result)}
            await super().emit(session, message, client)

    return _ServerScreenRpc


def _direct_stream(gateway, result: dict) -> dict:
    """Let an observe ticket open the stream on this machine's public address."""
    import os

    from flowly.gateway.display import _FLY_APP_NAME, STREAM_PATH

    app = os.environ.get("FLY_APP_NAME", "").strip()
    ticket, lifetime = result.get("ticket"), result.get("expiresInMs")
    registry = getattr(gateway, "_server_screen_tickets", None)
    if not _FLY_APP_NAME.match(app) or registry is None or not isinstance(ticket, str) or not isinstance(lifetime, int):
        return result
    registry.remember(ticket, lifetime / 1000)
    return {**result, "streamUrl": f"wss://{app}.fly.dev{STREAM_PATH}"}


def _with_exact_times(history: dict[str, Any]) -> dict[str, Any]:
    """History rows with ``timestampMs``: the stored time in epoch milliseconds.

    Sessions store ``datetime.now().isoformat()``, this machine's local time
    with no zone. Read elsewhere (the server runs in UTC) it would shift by
    the machine's offset; here it is read as what it is.
    """
    from datetime import datetime

    rows = history.get("messages")
    if not isinstance(rows, list):
        return history
    exact = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("timestamp"), str):
            try:
                stamp = datetime.fromisoformat(row["timestamp"])
                row = {**row, "timestampMs": int(stamp.astimezone().timestamp() * 1000)}
            except ValueError:
                pass
        exact.append(row)
    return {**history, "messages": exact}


class FlowlyServerChannel(WebChannel):
    """``WebChannel`` connected to the Flowly server instead of the relay."""

    def __init__(self, config: FlowlyServerChannelConfig, bus: MessageBus):
        self.server_config = config
        url, _, host = self._settings(config)
        super().__init__(WebChannelConfig(enabled=config.enabled, relay_url=url, server_id=host), bus)
        self._power = None
        self._power_tasks: set[asyncio.Task] = set()
        self._power_watch: asyncio.Task | None = None
        # profile → the server's delivery session for its chat; read from disk once.
        self._profile_server_sessions: dict[str, str] | None = None

    # ── one chat per agent (server PROTOCOL A7, B7) ────────────────────────

    def _server_capabilities(self) -> dict[str, Any]:
        # The server routes a named agent's chat here only when it sees this.
        return {"conversations": {"version": 2}}

    @staticmethod
    def _profile_sessions_path() -> Path:
        from flowly.profile import get_flowly_home

        return get_flowly_home() / "flowly_server" / "profile_sessions.json"

    def _profile_sessions(self) -> dict[str, str]:
        """Each named agent's delivery session, kept across restarts.

        Core learns it from the server's first delivery to that agent. An
        agent also speaks by itself (its introduction, a goal's next step,
        a scheduled check-in); after a restart those turns still reach the
        server only because the session was written down.
        """
        if self._profile_server_sessions is None:
            sessions: dict[str, str] = {}
            try:
                raw = json.loads(self._profile_sessions_path().read_text())
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                for profile, session_id in raw.items():
                    match = _PROFILE_DELIVERY_SESSION.match(session_id) if isinstance(session_id, str) else None
                    if match and match.group(1) == profile:
                        sessions[profile] = session_id
            self._profile_server_sessions = sessions
        return self._profile_server_sessions

    def _remember_profile_session(self, profile: str, session_id: str) -> None:
        match = _PROFILE_DELIVERY_SESSION.match(session_id)
        if not match or match.group(1) != profile:
            return
        sessions = self._profile_sessions()
        if sessions.get(profile) == session_id:
            return
        sessions[profile] = session_id
        path = self._profile_sessions_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sessions, sort_keys=True))
            os.replace(tmp, path)
        except OSError as exc:
            # Live delivery still works; only an agent's own turns after a restart would miss the server.
            logger.warning("[FlowlyServer] profile session not saved: {}", exc)

    async def _dispatch_rpc(self, ws, msg: dict) -> None:
        params = msg.get("params")
        profile = params.get("profile") if isinstance(params, dict) else None
        method = msg.get("method")
        if (method in PROFILE_CHAT_METHODS or method in PROFILE_SECRET_METHODS) and profile not in (None, "", "default"):
            await self._dispatch_profile_chat(ws, msg, profile)
            return
        await super()._dispatch_rpc(ws, msg)

    async def _dispatch_profile_chat(self, ws, msg: dict, profile: Any) -> None:
        """A named agent's chat call from the server: run it in that agent.

        Unlike ``profiles.rpc`` it binds no ``profile.event`` copies to the
        calling session (the server fans the chat out itself); a delivery
        names the session the agent's chat events go to (B7).
        """
        from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc

        rpc_id = msg.get("id", "")
        session_id = msg.get("sessionId", "")
        method = msg["method"]

        async def reply(body: dict[str, Any]) -> None:
            await ws.send(json.dumps({"type": "rpc", "id": rpc_id, "sessionId": session_id, **body}))

        host = self._profile_host
        if not isinstance(profile, str) or not _PROFILE_NAME.match(profile):
            await reply({"error": {"code": "INVALID_PARAMS", "message": "Unknown agent.", "retryable": False}})
            return
        if host is None:
            await reply({"error": {"code": "PROFILE_HOST_UNAVAILABLE",
                                   "message": "This agent host does not manage isolated profiles.", "retryable": False}})
            return
        inner = {key: value for key, value in msg["params"].items() if key != "profile"}
        try:
            method, inner = validate_profile_rpc(method, inner)
        except ProfileHostError as exc:
            await reply({"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}})
            return
        if method in ("chat.send", "chat.inflight") and isinstance(session_id, str):
            # Before dispatch: the first event may follow the acknowledgement at once.
            # The server also names the session with a read-only chat.inflight when it
            # learns the agent, so the agent's words before its first message (its
            # introduction) reach the server too. Only `srv-p:` sessions are kept.
            self._remember_profile_session(profile, session_id)
        try:
            result = await host.dispatch("profiles.rpc", {"name": profile, "method": method, "params": inner})
        except ProfileHostError as exc:
            await reply({"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}})
            return
        except FileNotFoundError:
            await reply({"error": {"code": "PROFILE_NOT_FOUND", "message": "The agent no longer exists.", "retryable": False}})
            return
        except Exception as exc:
            logger.error("[FlowlyServer] {} for agent {} failed ({})", method, profile, type(exc).__name__)
            await reply({"error": {"code": "INTERNAL", "message": "The agent could not take this request.", "retryable": True}})
            return
        session_key = str(inner.get("sessionKey") or "")
        if method == "chat.history" and isinstance(result, dict):
            result = _with_exact_times(result)
        if method in {"chat.send", "chat.inflight"} and session_key and isinstance(result, dict):
            # Events that name only their run still find the conversation.
            receipt = result if method == "chat.send" else result.get("inflight")
            run_id = str(receipt.get("runId") or "") if isinstance(receipt, dict) else ""
            if run_id:
                self._profile_run_bindings[(profile, run_id)] = (session_key, time.monotonic())
                self._prune_profile_bindings()
        await reply({"result": result})

    async def report_profile_home_message(self, profile: str, message_id: str, text: str, files: int) -> None:
        """A person's message in a named agent's one chat that did not come
        through the server (Desktop, a direct gateway app), told to it on the
        agent's delivery session as the person's own entry (server
        chat-one-log.md §10.9). The server keeps it once; a message it
        delivered itself is in its log already and stays as it is."""
        from flowly.agent_home import HOME_SESSION

        session_id = self._profile_sessions().get(profile)
        if not session_id or not message_id:
            return
        await self._send_or_queue(json.dumps({
            "type": "event", "sessionId": session_id, "event": "chat", "profile": profile,
            "data": {"state": "user", "sessionKey": HOME_SESSION, "messageId": message_id,
                     "message": {"role": "user", "text": text, "files": files}},
        }))

    async def _forward_profile_event_to_server(
        self, profile: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        """A named agent's own chat, once, under its profile (B7).

        Only its one conversation (the home session), only to the session the
        server delivered its messages on, and in the shape the main agent's
        chat has on this socket: a stream delta is ``chat`` ``streaming``.
        """
        from flowly.agent_home import HOME_SESSION

        if event_type == "agent.exchange":
            # A message between two agents (any two, the main agent too), once,
            # for the server to record and show (agent-exchanges.md). The inbox
            # turn behind it never leaves the host.
            await self._send_or_queue(json.dumps({"type": "event", "event": "agent.exchange", "data": payload}))
            return
        if profile in ("", "default") or event_type not in PROFILE_CHAT_EVENTS:
            return
        if payload.get("sessionKey") != HOME_SESSION:
            return
        run_id = str(payload.get("runId") or "")
        if run_id and (profile, run_id) not in self._profile_run_bindings:
            # A turn the agent started itself (no chat.send bound it): its
            # later events that name only the run still find this chat.
            self._profile_run_bindings[(profile, run_id)] = (HOME_SESSION, time.monotonic())
        session_id = self._profile_sessions().get(profile)
        if not session_id:
            return
        name, data = event_type, payload
        if event_type == "chat" and payload.get("state") == "final" and not payload.get("streamRunId") and payload.get("runId"):
            # The direct gateway's final names the turn (= the person's message
            # id) as `runId`; the main agent's names its stored reply there and
            # the turn as `streamRunId` (wire §2). Sent as is, the server would
            # store the reply over the person's message.
            data = {**payload, "streamRunId": payload["runId"], "runId": f"{payload['runId']}:reply"}
        if event_type == "agent":
            text = payload.get("data", {}).get("text") if isinstance(payload.get("data"), dict) else None
            if payload.get("stream") != "assistant" or not isinstance(text, str):
                return
            name = "chat"
            data = {
                "state": "streaming", "runId": payload.get("runId") or "", "sessionKey": HOME_SESSION, "delta": text,
                **({"seq": payload["seq"]} if isinstance(payload.get("seq"), int) else {}),
                **({"turnOrigin": payload["turnOrigin"]} if isinstance(payload.get("turnOrigin"), str) else {}),
            }
        await self._send_or_queue(json.dumps({
            "type": "event", "sessionId": session_id, "event": name, "profile": profile, "data": data,
        }))

    # ── connection ────────────────────────────────────────────────────────

    @staticmethod
    def _settings(config: FlowlyServerChannelConfig) -> tuple[str, str, str]:
        return config.connection_settings()

    def connection_settings(self) -> tuple[str, str, str]:
        """``(server_url, agent_secret, agent_host_id)``, config first, then env."""
        return self._settings(self.server_config)

    def _ready_to_connect(self) -> bool:
        if not self.server_config.enabled:
            return False
        url, secret, host = self.connection_settings()
        missing = [name for name, value in (("serverUrl", url), ("agentSecret", secret), ("agentHostId", host))
                   if not value]
        if missing:
            logger.error(f"[FlowlyServer] {', '.join(missing)} not set — cannot connect to the Flowly server")
            return False
        if not url.startswith(FLOWLY_SERVER_URL_PREFIXES):
            logger.error("[FlowlyServer] serverUrl must use wss:// (ws:// only to this machine)")
            return False
        self.config.relay_url, self.config.server_id = url, host
        return True

    def _relay_connection(self) -> tuple[str, dict[str, Any], Callable[[], str] | None]:
        url, secret, host = self.connection_settings()
        # No screen-stream token: in v1 the screen does not travel over this
        # socket (PROTOCOL.md C, the client pulls it from the machine).
        return url, {"additional_headers": {
            "Authorization": f"Bearer {secret}",
            "X-Flowly-Agent-Host": host,
            "X-Flowly-Agent-Features": VOICE_AUTHORITY_FEATURE,
        }}, None

    def _on_connected(self, ws) -> None:
        if self._power is not None:
            self._power.mark_connected()
        if self._screen_gateway is not None:
            # The relay's screen RPC proxy, on this connection (PROTOCOL.md C).
            # The relay builds it only beside its own stream transport; here the
            # stream comes straight from this machine, so only the RPCs ride the
            # server's socket.
            self._screen_rpc = _server_screen_rpc_class()(
                gateway=self._screen_gateway, current=lambda: self._ws is ws,
                send=lambda frame: ws.send(json.dumps(frame)),
            )

    async def _reconnect_wait(self, delay: float) -> bool:
        """Back off, but reconnect at once after a resume.

        The server drops the socket as it suspends the machine, so the backoff
        is usually running when the machine freezes; the suspend stops the
        monotonic clock that ``asyncio.sleep`` uses, and the rest of it would
        be waited out after the wake, while the server holds the person's
        message. A wall-clock jump between short ticks shows the resume.
        """
        deadline = time.monotonic() + delay
        wall, mono = time.time(), time.monotonic()
        while (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(RESUME_TICK_SECONDS, left))
            now_wall, now_mono = time.time(), time.monotonic()
            if (now_wall - wall) - (now_mono - mono) > RESUME_JUMP_SECONDS:
                logger.info("[FlowlyServer] resumed after a suspend; reconnecting now")
                if self._power is not None:
                    self._power.wake()
                return True
            wall, mono = now_wall, now_mono
        return False

    async def start(self) -> None:
        if self._power is not None and self._power_watch is None:
            self._power_watch = asyncio.create_task(self._power.watch(), name="flowly-server-power-watch")
        try:
            await super().start()
        finally:
            if not self._running:
                self._stop_power_watch()

    async def stop(self) -> None:
        self._stop_power_watch()
        for task in tuple(self._power_tasks):
            task.cancel()
        await asyncio.gather(*self._power_tasks, return_exceptions=True)
        await super().stop()

    def _stop_power_watch(self) -> None:
        if self._power_watch is not None:
            self._power_watch.cancel()
            self._power_watch = None

    # ── power ─────────────────────────────────────────────────────────────

    def set_power_monitor(self, monitor) -> None:
        """Attach the gateway's ``PowerMonitor`` (flowly/power/monitor.py)."""
        self._power = monitor
        monitor.add_next_due_listener(self._send_next_due)

    async def _send_next_due(self, next_due_at: int | None) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"type": "power.next_due", "nextDueAt": next_due_at}))
        except Exception as exc:  # noqa: BLE001 - the next connect announces it again
            logger.debug(f"[FlowlyServer] power.next_due not sent: {exc}")

    async def wait_for_power_tasks(self) -> None:
        """Wait for background power RPCs (tests, shutdown)."""
        while self._power_tasks:
            await asyncio.gather(*tuple(self._power_tasks), return_exceptions=True)

    @staticmethod
    def _reply(msg: dict, body: dict) -> str:
        frame: dict[str, Any] = {"type": "rpc", "id": msg.get("id", "")}
        if msg.get("sessionId"):
            frame["sessionId"] = msg["sessionId"]
        frame.update(body)
        return json.dumps(frame)

    async def _handle_power_rpc(self, ws, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if msg.get("sessionId"):
            # A client connection reached a server-only method; never obey it.
            await ws.send(self._reply(msg, {"error": {
                "code": "FORBIDDEN", "message": "Only the Flowly server can manage power."}}))
            return
        if self._power is None:
            await ws.send(self._reply(msg, {"error": {
                "code": "POWER_UNAVAILABLE", "message": "This agent does not manage power."}}))
            return
        idle_minutes = params.get("idleMinutes") if isinstance(params, dict) else None
        if not isinstance(params, dict) or (
            idle_minutes is not None
            and (type(idle_minutes) not in (int, float) or not 0 <= idle_minutes <= 7 * 24 * 60)
        ):
            await ws.send(self._reply(msg, {"error": {
                "code": "INVALID_REQUEST", "message": "idleMinutes must be a number of minutes."}}))
            return
        if method == "power.status":
            result = self._power.status(idle_minutes)
        elif method == "power.sleep":
            result = await self._power.sleep(idle_minutes)
        elif method == "power.wake":
            self._power.wake()
            result = {"ok": True}
        else:  # heartbeat.trigger: a whole agent turn; keep receiving meanwhile
            task = asyncio.create_task(self._trigger_heartbeat(ws, msg), name="flowly-server-heartbeat")
            self._power_tasks.add(task)
            task.add_done_callback(self._power_tasks.discard)
            return
        await ws.send(self._reply(msg, {"result": result}))

    async def _trigger_heartbeat(self, ws, msg: dict) -> None:
        try:
            result = await self._power.trigger_heartbeat()
            body: dict[str, Any] = {"result": result}
        except Exception as exc:  # noqa: BLE001 - the server must get an answer
            logger.warning(f"[FlowlyServer] heartbeat.trigger failed: {exc}")
            body = {"error": {"code": "INTERNAL", "message": "The heartbeat could not run."}}
        try:
            await ws.send(self._reply(msg, body))
        except Exception as exc:  # noqa: BLE001 - socket may have dropped
            logger.debug(f"[FlowlyServer] heartbeat.trigger reply not sent: {exc}")

    # ── screen ────────────────────────────────────────────────────────────

    async def _handle_screen_ticket(self, ws, msg: dict) -> None:
        """PROTOCOL.md C: a ticket the client presents to this machine's screen stream."""
        from flowly.desktop_runtime.authority import ScreenError
        from flowly.gateway.display import issue_server_ticket

        params = msg.get("params")
        profile = params.get("profile", "default") if isinstance(params, dict) else "default"
        try:
            if self._screen_gateway is None:
                raise ScreenError("SCREEN_TICKET_UNAVAILABLE")
            body: dict[str, Any] = {"result": await asyncio.to_thread(
                issue_server_ticket, self._screen_gateway, profile)}
        except ScreenError as exc:
            message = ("This agent cannot share its screen directly."
                       if exc.code == "SCREEN_TICKET_UNAVAILABLE" else "The screen ticket could not be issued.")
            body = {"error": {"code": exc.code, "message": message, "retryable": False}}
        except OSError:
            body = {"error": {"code": "CONTROL_UNAVAILABLE", "message": "The screen state is unavailable.",
                              "retryable": True}}
        await ws.send(self._reply(msg, body))

    async def _handle_screen_status(self, ws, msg: dict) -> None:
        """PROTOCOL.md C: the screen's phase, so a client knows when a ticket is worth asking for."""
        from flowly.desktop_runtime.authority import ScreenError
        from flowly.gateway.display import server_screen_status

        params = msg.get("params")
        profile = params.get("profile", "default") if isinstance(params, dict) else "default"
        try:
            if self._screen_gateway is None:
                raise ScreenError("SCREEN_TICKET_UNAVAILABLE")
            body: dict[str, Any] = {"result": await asyncio.to_thread(
                server_screen_status, self._screen_gateway, profile)}
        except ScreenError as exc:
            body = {"error": {"code": exc.code, "message": "The screen state is unavailable.", "retryable": False}}
        except OSError:
            body = {"error": {"code": "CONTROL_UNAVAILABLE", "message": "The screen state is unavailable.",
                              "retryable": True}}
        await ws.send(self._reply(msg, body))

    # ── frames ────────────────────────────────────────────────────────────

    def _decode_relay_message(self, ws, msg: object) -> dict | None:
        """The relay's rule, except for the server's own calls (PROTOCOL H1).

        After the voice identity handshake a client's frames arrive only in
        signed ``relay.browser`` envelopes and an unwrapped one is dropped,
        as with the relay. Unlike the relay, the server also calls this agent
        itself, unwrapped: power and provider calls carry no ``sessionId``,
        inbox ``chat.send`` deliveries a ``srv-`` one (client connections are
        ``c-``). They run as the host, as before; one that names an account
        certificate is refused later, having no attested sender.
        """
        if (self._relay_authority_enabled and type(msg) is dict and msg.get("type") == "rpc"
                and self._ws is ws and _server_own_call(msg)):
            return msg
        return super()._decode_relay_message(ws, msg)

    async def _handle_relay_message(self, ws, msg: dict) -> None:
        # Decoded once here (a verified envelope comes back as itself), so the
        # power and screen rules below also see a client's wrapped request.
        msg = self._decode_relay_message(ws, msg)
        if msg is None:
            return
        if isinstance(msg, dict) and msg.get("type") == "rpc":
            method = msg.get("method")
            method = method if isinstance(method, str) else ""
            if method in POWER_METHODS:
                await self._handle_power_rpc(ws, msg)
                return
            if self._power is not None and self._power.sleeping:
                await ws.send(self._reply(msg, {"error": dict(SLEEPING_ERROR)}))
                return
            if self._power is not None and not _is_passive(method):
                self._power.touch()
            if method == "screen.ticket":
                await self._handle_screen_ticket(ws, msg)
                return
            if method == "screen.status":
                await self._handle_screen_status(ws, msg)
                return
        await super()._handle_relay_message(ws, msg)
        if isinstance(msg, dict) and msg.get("type") == "ready" and ws is self._ws:
            if self._screen_rpc is not None:
                self._screen_rpc.enabled = True
            if self._power is not None:
                await self._power.check_next_due(force=True)
