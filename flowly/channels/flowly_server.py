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
import time
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
})

#: While backing off, look at the clocks this often for a resume.
RESUME_TICK_SECONDS = 0.5
#: The wall clock ran this much further than the monotonic one within a tick:
#: the machine was suspended (or the clock stepped; reconnecting early is harmless).
RESUME_JUMP_SECONDS = 2.0

SLEEPING_ERROR = {
    "code": "AGENT_SLEEPING",
    "message": "The agent is going to sleep. Try again after it wakes.",
    "retryable": True,
}


def _is_passive(method: str) -> bool:
    return method in PASSIVE_METHODS or method.endswith(".status")


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


class FlowlyServerChannel(WebChannel):
    """``WebChannel`` connected to the Flowly server instead of the relay."""

    def __init__(self, config: FlowlyServerChannelConfig, bus: MessageBus):
        self.server_config = config
        url, _, host = self._settings(config)
        super().__init__(WebChannelConfig(enabled=config.enabled, relay_url=url, server_id=host), bus)
        self._power = None
        self._power_tasks: set[asyncio.Task] = set()
        self._power_watch: asyncio.Task | None = None

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

    async def _handle_relay_message(self, ws, msg: dict) -> None:
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
