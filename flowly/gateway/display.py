"""Authenticated direct display admission and bounded binary RFB transport.

The installed host, profile directory and Unix socket are server-resolved.
Stream credentials travel in headers, not URLs, chat events or access logs.
This first transport slice is viewing-only; input/control RPCs are advertised
only after the host input-drain coordinator is connected.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from pathlib import Path

from aiohttp import WSMsgType, web

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.rfb import RfbClientGate
from flowly.desktop_runtime.tickets import StreamScope, StreamTickets
from flowly.gateway.auth import host_origin_allowed
from flowly.gateway.mcp_management import _protected_socket

METHODS = frozenset({"display.capabilities", "display.status", "display.observe"})
STREAM_PATH = "/api/display/stream"
MAX_FRAME = 256 * 1024
MAX_STREAMS = 4


class DisplaySession:
    def __init__(self, identity: dict[str, str], directory: Path):
        self.identity = dict(identity)
        self.directory = directory
        self._runtime: ScreenRuntime | None = None
        self._tickets: StreamTickets | None = None
        self.streams: set[web.WebSocketResponse] = set()
        self.attaching = 0

    @property
    def runtime(self) -> ScreenRuntime:
        if self._runtime is None:
            self._runtime = ScreenRuntime(self.directory)
        return self._runtime

    @property
    def tickets(self) -> StreamTickets:
        if self._tickets is None:
            self._tickets = StreamTickets(self.directory / "admission")
        return self._tickets

    def capabilities(self) -> dict:
        capability = ScreenRuntime.capabilities()
        return {**self.identity, "protocolVersion": 1, **capability,
                "transports": ["direct"] if capability["supported"] else [],
                "view": capability["supported"], "input": False,
                "lifecycle": False, "installSupported": False}

    def scope(self, generation: str) -> StreamScope:
        # Every token-authenticated connection to this installation represents
        # its owner. A renderer-supplied owner/viewer ID is never authority.
        return StreamScope(self.identity["hostId"], self.identity["hostId"],
                           self.identity["profileId"], generation)

    def current(self, generation: str) -> dict:
        state = self.runtime.status()
        if not state or state["phase"] != "running":
            raise ScreenError("SCREEN_NOT_RUNNING")
        if state["generation"] != generation:
            raise ScreenError("STALE_GENERATION")
        control = self.runtime.authority.snapshot()
        if control["generation"] != generation or not control["running"]:
            raise ScreenError("STALE_GENERATION")
        if control["control"] != "agent":
            raise ScreenError("HUMAN_HAS_CONTROL")
        return state

    def status(self) -> dict:
        capability = self.capabilities()
        if not capability["supported"]:
            return {**capability, "phase": "unavailable", "generation": None, "control": None}
        state = self.runtime.status()
        return {**capability, "phase": state["phase"] if state else "stopped",
                "generation": state["generation"] if state else None,
                "error": state.get("error") if state else None,
                "control": self.runtime.authority.snapshot(), "width": 1440, "height": 900}

    def observe(self, generation: str) -> dict:
        capability = self.capabilities()
        if not capability["supported"]:
            raise ScreenError(capability["reason"])
        self.current(generation)
        viewer = secrets.token_urlsafe(24)
        ticket = self.tickets.issue(self.scope(generation), viewer)
        return {**self.identity, "protocolVersion": 1, "generation": generation,
                "viewerId": viewer, "ticket": ticket, "expiresInMs": 30_000,
                "streamPath": STREAM_PATH, "viewOnly": True}


def _resolve(gateway, params: dict, *, pinned: bool = False) -> DisplaySession:
    with gateway._display_resolution_lock:
        return _resolve_locked(gateway, params, pinned=pinned)


def _resolve_locked(gateway, params: dict, *, pinned: bool = False) -> DisplaySession:
    from flowly.profile import (
        current_profile_name,
        ensure_profile_bot_id,
        get_flowly_home,
        get_or_create_profile_host_id,
    )

    profile = params.get("profileId", current_profile_name())
    if not isinstance(profile, str) or not profile or len(profile) > 64:
        raise ScreenError("INVALID_PARAMS")
    if gateway.profile_host is None and profile != current_profile_name():
        raise ScreenError("PROFILE_NOT_AVAILABLE")
    try:
        info = ensure_profile_bot_id(profile)
    except (ValueError, FileNotFoundError):
        raise ScreenError("PROFILE_NOT_AVAILABLE") from None
    # A process started under a custom FLOWLY_HOME must never silently expose
    # a different default directory. Custom-home identity migration is separate.
    if profile == current_profile_name() and info.path.resolve() != get_flowly_home().resolve():
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    host = gateway.profile_host.host_id if gateway.profile_host else get_or_create_profile_host_id()
    identity = {"hostId": host, "profileId": profile, "botId": info.bot_id, "screenId": "browser"}
    guarded = pinned or "expectedHostId" in params or "expectedBotId" in params
    if guarded and (params.get("expectedHostId") != host or params.get("expectedBotId") != info.bot_id):
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    key = (profile, info.bot_id)
    sessions = gateway._display_sessions
    if key not in sessions:
        # Bound stale cached identities after profile deletion/recreation.
        stale = [key for key, session in sessions.items() if not session.streams and not session.attaching]
        for old in stale:
            if old[0] == profile or len(sessions) >= 16:
                sessions.pop(old, None)
        if len(sessions) >= 16:
            raise ScreenError("SCREEN_BUSY")
        sessions[key] = DisplaySession(identity, info.path / "desktop")
    return sessions[key]


def _error(code: str, status: int = 400) -> web.Response:
    return web.json_response({"error": {"code": code}}, status=status, headers={"Cache-Control": "no-store"})


async def handle_rpc(gateway, ws, client_id: str, rpc_id: str, method: str, params) -> None:
    request = getattr(ws, "_req", None)
    if not gateway._require_auth or gateway._ws_clients.get(client_id) is not ws or ws.closed:
        await gateway._ws_rpc_error(ws, rpc_id, "UNAUTHORIZED", "An authenticated display connection is required.")
        return
    if request is None or not _protected_socket(request) or not host_origin_allowed(request):
        await gateway._ws_rpc_error(ws, rpc_id, "SECURE_TRANSPORT_REQUIRED", "Use TLS or an SSH tunnel for the screen.")
        return
    try:
        if method not in METHODS:
            raise ScreenError("METHOD_NOT_ALLOWED")
        allowed = {"profileId", "expectedHostId", "expectedBotId"}
        if method == "display.observe":
            allowed.add("generation")
        if not isinstance(params, dict) or set(params) - allowed:
            raise ScreenError("INVALID_PARAMS")
        session = await asyncio.to_thread(_resolve, gateway, params, pinned=method == "display.observe")
        if method == "display.capabilities":
            result = session.capabilities()
        elif method == "display.status":
            result = await asyncio.to_thread(session.status)
        else:
            generation = params.get("generation")
            if not isinstance(generation, str) or not generation or len(generation) > 256:
                raise ScreenError("INVALID_PARAMS")
            result = await asyncio.to_thread(session.observe, generation)
        await gateway._ws_rpc_reply(ws, rpc_id, result)
    except ScreenError as exc:
        await gateway._ws_rpc_error(ws, rpc_id, exc.code, "The screen request could not be completed.")
    except OSError:
        await gateway._ws_rpc_error(ws, rpc_id, "CONTROL_UNAVAILABLE", "The screen state is unavailable.")


def register_display(app: web.Application, gateway) -> None:
    gateway._display_sessions = {}
    gateway._display_resolution_lock = threading.Lock()

    async def stream(request: web.Request) -> web.StreamResponse:
        # The ordinary gateway middleware checks Authorization. Unlike /ws,
        # this route is never exempted from that check, even with a ticket.
        if not gateway._require_auth:
            return _error("UNAUTHORIZED", 401)
        if not _protected_socket(request) or not host_origin_allowed(request):
            return _error("SECURE_TRANSPORT_REQUIRED", 403)
        if request.query_string:
            return _error("INVALID_PARAMS")
        params = {"profileId": request.headers.get("X-Flowly-Profile", "default"),
                  "expectedHostId": request.headers.get("X-Flowly-Host-Id"),
                  "expectedBotId": request.headers.get("X-Flowly-Bot-Id")}
        generation = request.headers.get("X-Flowly-Screen-Generation", "")
        ticket = request.headers.get("X-Flowly-Screen-Ticket", "")
        try:
            session = await asyncio.to_thread(_resolve, gateway, params, pinned=True)
            await asyncio.to_thread(session.tickets.consume, ticket, session.scope(generation))
            await asyncio.to_thread(session.current, generation)
        except ScreenError as exc:
            return _error(exc.code, 403)
        except OSError:
            return _error("CONTROL_UNAVAILABLE", 503)
        if session.attaching >= MAX_STREAMS:
            return _error("SCREEN_BUSY", 429)
        session.attaching += 1
        writer = None
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=15, compress=False, timeout=2)
        tasks = []
        try:
            socket = session.runtime.socket_directory / "rfb.sock"
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(socket, limit=128 * 1024), 5)
            writer.transport.set_write_buffer_limits(high=128 * 1024, low=32 * 1024)
            await ws.prepare(request)
            session.streams.add(ws)
            gate = RfbClientGate()

            async def check_current():
                fresh = await asyncio.to_thread(_resolve, gateway, params, pinned=True)
                if fresh is not session:
                    raise ScreenError("PROFILE_IDENTITY_CHANGED")
                await asyncio.to_thread(session.current, generation)

            async def server_to_viewer():
                while data := await reader.read(64 * 1024):
                    await check_current()
                    await asyncio.wait_for(ws.send_bytes(data), 5)

            async def viewer_to_server():
                async for message in ws:
                    if message.type != WSMsgType.BINARY:
                        raise ScreenError("INVALID_RFB_STREAM")
                    await check_current()
                    data = gate.feed(message.data, lambda: False)
                    if data:
                        writer.write(data)
                        await asyncio.wait_for(writer.drain(), 5)

            async def monitor():
                while True:
                    await asyncio.sleep(0.25)
                    await check_current()

            tasks = [asyncio.create_task(operation()) for operation in (server_to_viewer, viewer_to_server, monitor)]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ScreenError, OSError, asyncio.TimeoutError):
            if not ws.prepared:
                return _error("TRANSPORT_UNAVAILABLE", 503)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if writer is not None:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 2)
                    except (OSError, asyncio.TimeoutError):
                        writer.transport.abort()
                if ws.prepared:
                    await ws.close(code=1000, message=b"Screen disconnected")
            finally:
                session.streams.discard(ws)
                session.attaching -= 1
        return ws

    async def shutdown(_app):
        await asyncio.gather(*(ws.close(code=1001) for session in list(gateway._display_sessions.values())
                               for ws in tuple(session.streams)), return_exceptions=True)

    app.router.add_get(STREAM_PATH, stream)
    app.on_shutdown.append(shutdown)
