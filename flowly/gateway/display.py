"""Authenticated direct display admission and bounded binary RFB transport.

The installed host, profile directory and Unix socket are server-resolved.
Stream credentials travel in headers, not URLs, chat events or access logs.
Input travels in epoch-tagged frames and only through the supervisor's broker.
Legacy raw RFB streams remain viewing-only even when a viewer holds control.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import threading
from pathlib import Path

from aiohttp import WSMsgType, web

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.input_service import InputServiceClient
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.rfb import RfbClientGate
from flowly.desktop_runtime.tickets import StreamScope, StreamTickets
from flowly.desktop_runtime.viewers import ViewerCredentials
from flowly.gateway.auth import host_origin_allowed
from flowly.gateway.mcp_management import _protected_socket

METHODS = frozenset({"display.capabilities", "display.status", "display.observe", "display.viewer.create",
                     "display.control.acquire", "display.control.release", "display.control.reclaim"})
STREAM_PATH = "/api/display/stream"
MAX_FRAME = 256 * 1024
MAX_STREAMS = 4
INPUT_PROTOCOL = "epoch-u64-v1"


def _supports_input(state: dict | None) -> bool:
    return bool(state and state.get("phase") == "running" and state.get("inputServiceVersion") == 2
                and state.get("observerServiceVersion") == 2)


class DisplaySession:
    def __init__(self, identity: dict[str, str], directory: Path):
        self.identity = dict(identity)
        self.directory = directory
        self._runtime: ScreenRuntime | None = None
        self._tickets: StreamTickets | None = None
        self._viewers: ViewerCredentials | None = None
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
        state = self.runtime.status() if capability["supported"] else None
        return {**self.identity, "protocolVersion": 1, **capability,
                "transports": ["direct"] if capability["supported"] else [],
                "view": capability["supported"], "input": _supports_input(state),
                "lifecycle": False, "installSupported": False}

    @property
    def viewers(self) -> ViewerCredentials:
        if self._viewers is None:
            self._viewers = ViewerCredentials(self.tickets)
        return self._viewers

    def running(self, generation: str) -> dict:
        state = self.runtime.status()
        if not state or state["phase"] != "running":
            raise ScreenError("SCREEN_NOT_RUNNING")
        if state["generation"] != generation:
            raise ScreenError("STALE_GENERATION")
        return state

    def scope(self, generation: str) -> StreamScope:
        # Every token-authenticated connection to this installation represents
        # its owner. A renderer-supplied owner/viewer ID is never authority.
        return StreamScope(self.identity["hostId"], self.identity["hostId"],
                           self.identity["profileId"], generation)

    def current(self, generation: str, viewer: str | None = None) -> dict:
        state = self.running(generation)
        control = self.runtime.authority.snapshot()
        if control["generation"] != generation or not control["running"]:
            raise ScreenError("STALE_GENERATION")
        if control["control"] != "agent" and (viewer is None or not self.runtime.authority.may_observe(viewer, generation)):
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

    def create_viewer(self, generation: str) -> dict:
        self.running(generation)
        return {**self.identity, "generation": generation,
                **self.viewers.create(self.scope(generation), self.identity["botId"])}

    def authenticate_viewer(self, generation: str, token: str) -> str:
        return self.viewers.authenticate(token, self.scope(generation), self.identity["botId"])

    def observe(self, generation: str, token: str | None = None) -> dict:
        capability = self.capabilities()
        if not capability["supported"]:
            raise ScreenError(capability["reason"])
        credentials = {}
        if token is None:
            self.current(generation)
            credentials = self.create_viewer(generation)
            viewer = credentials["viewerId"]
        else:
            viewer = self.authenticate_viewer(generation, token)
            self.current(generation, viewer)
        ticket = self.tickets.issue(self.scope(generation), viewer)
        return {**credentials, **self.identity, "protocolVersion": 1, "generation": generation,
                "viewerId": viewer, "ticket": ticket, "expiresInMs": 30_000,
                "streamPath": STREAM_PATH, "viewOnly": True,
                "inputProtocol": INPUT_PROTOCOL if _supports_input(self.running(generation)) else None}

    def control(self, method: str, generation: str, token: str, epoch: int, operation: str) -> dict:
        state = self.running(generation)
        if state.get("observerServiceVersion") not in (1, 2) or state.get("inputServiceVersion") not in (1, 2):
            raise ScreenError("SCREEN_UPGRADE_REQUIRED")
        viewer = self.authenticate_viewer(generation, token)
        result = InputServiceClient(self.runtime.socket_directory / "input.sock").request(
            method.removeprefix("display."), generation=generation, viewer=viewer,
            epoch=epoch, operationId=operation,
        )
        return {**self.identity, "generation": generation, "control": result}


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
        pinned = method not in {"display.capabilities", "display.status"}
        if pinned:
            allowed.add("generation")
        if method == "display.observe":
            allowed.add("viewerToken")
        if method.startswith("display.control."):
            allowed.update({"viewerToken", "epoch", "operationId"})
        if not isinstance(params, dict) or set(params) - allowed:
            raise ScreenError("INVALID_PARAMS")
        session = await asyncio.to_thread(_resolve, gateway, params, pinned=pinned)
        if method == "display.capabilities":
            result = session.capabilities()
        elif method == "display.status":
            result = await asyncio.to_thread(session.status)
        else:
            generation = params.get("generation")
            if not isinstance(generation, str) or not generation or len(generation) > 256:
                raise ScreenError("INVALID_PARAMS")
            if method == "display.observe":
                result = await asyncio.to_thread(session.observe, generation, params.get("viewerToken"))
            elif method == "display.viewer.create":
                result = await asyncio.to_thread(session.create_viewer, generation)
            else:
                result = await asyncio.to_thread(session.control, method, generation, params.get("viewerToken"),
                                                 params.get("epoch"), params.get("operationId"))
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
        input_protocol = request.headers.get("X-Flowly-Screen-Input")
        if input_protocol not in (None, INPUT_PROTOCOL):
            return _error("INVALID_PARAMS")
        try:
            session = await asyncio.to_thread(_resolve, gateway, params, pinned=True)
            viewer = await asyncio.to_thread(session.tickets.consume, ticket, session.scope(generation))
            if not await asyncio.to_thread(session.viewers.active, viewer, session.scope(generation),
                                           session.identity["botId"]):
                raise ScreenError("VIEWER_EXPIRED")
            state = await asyncio.to_thread(session.current, generation, viewer)
            if input_protocol and not _supports_input(state):
                raise ScreenError("SCREEN_UPGRADE_REQUIRED")
        except ScreenError as exc:
            return _error(exc.code, 403)
        except OSError:
            return _error("CONTROL_UNAVAILABLE", 503)
        if session.attaching >= MAX_STREAMS:
            return _error("SCREEN_BUSY", 429)
        session.attaching += 1
        writer = None
        observer_writer = None
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME + (8 if input_protocol else 0), heartbeat=15,
                                   compress=False, timeout=2)
        tasks = []
        stream_id = secrets.token_urlsafe(24)
        try:
            if state.get("observerServiceVersion") in (1, 2):
                async with asyncio.timeout(3):
                    observer_reader, observer_writer = await asyncio.open_unix_connection(
                        session.runtime.socket_directory / "observers.sock", limit=4096)
                    registration = {"viewer": viewer, "generation": generation}
                    if state["observerServiceVersion"] == 2:
                        registration["stream"] = stream_id
                    hello = json.dumps(registration).encode()
                    observer_writer.write(len(hello).to_bytes(4, "big") + hello)
                    await observer_writer.drain()
                    size = int.from_bytes(await observer_reader.readexactly(4), "big")
                    if not 0 < size <= 4096:
                        raise ScreenError("CONTROL_UNAVAILABLE")
                    reply = json.loads(await observer_reader.readexactly(size))
                    if reply != {"ok": True}:
                        raise ScreenError("CONTROL_UNAVAILABLE")

                async def revoked():
                    await observer_reader.read(1)
                    raise ScreenError("VIEWER_REVOKED")

                tasks.append(asyncio.create_task(revoked()))
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
                await asyncio.to_thread(session.current, generation, viewer)

            async def server_to_viewer():
                while data := await reader.read(64 * 1024):
                    await check_current()
                    await asyncio.wait_for(ws.send_bytes(data), 5)

            async def viewer_to_server():
                input_client = InputServiceClient(session.runtime.socket_directory / "input.sock")
                input_state = None
                input_epoch = None
                async for message in ws:
                    if message.type != WSMsgType.BINARY:
                        raise ScreenError("INVALID_RFB_STREAM")
                    await check_current()
                    payload = message.data
                    epoch = None
                    if input_protocol:
                        if not 8 < len(payload) <= MAX_FRAME + 8:
                            raise ScreenError("INVALID_RFB_STREAM")
                        epoch = int.from_bytes(payload[:8], "big")
                        if epoch >= 2**53:
                            raise ScreenError("INVALID_RFB_STREAM")
                        payload = payload[8:]
                    inputs = []
                    allowed = bool(input_protocol and epoch and await asyncio.to_thread(
                        session.runtime.authority.may_input, viewer, generation, epoch))
                    data = gate.feed(payload, lambda: allowed, epoch=epoch,
                                     on_input=inputs.append if input_protocol else None)
                    if data:
                        writer.write(data)
                        await asyncio.wait_for(writer.drain(), 5)
                    if inputs:
                        if input_epoch != epoch:
                            input_state = await asyncio.to_thread(
                                input_client.request, "attach", viewer=viewer, generation=generation,
                                epoch=epoch, stream=stream_id,
                            )
                            input_epoch = epoch
                        batch = bytearray()
                        for item in [*inputs, b""]:
                            if batch and (not item or len(batch) + len(item) > 64 * 1024 + 8):
                                result = await asyncio.to_thread(
                                    input_client.request, "send", viewer=viewer, generation=generation, epoch=epoch,
                                    handle=input_state["handle"], sequence=input_state["nextSequence"],
                                    data=base64.b64encode(batch).decode(),
                                )
                                input_state["nextSequence"] = result["nextSequence"]
                                batch.clear()
                            batch.extend(item)

            async def monitor():
                while True:
                    await asyncio.sleep(0.25)
                    await check_current()
                    if not await asyncio.to_thread(session.viewers.active, viewer, session.scope(generation),
                                                   session.identity["botId"]):
                        raise ScreenError("VIEWER_EXPIRED")

            tasks.extend(asyncio.create_task(operation()) for operation in (server_to_viewer, viewer_to_server, monitor))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ScreenError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
            if not ws.prepared:
                return _error("TRANSPORT_UNAVAILABLE", 503)
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                if writer is not None:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 2)
                    except (OSError, asyncio.TimeoutError):
                        writer.transport.abort()
                if ws.prepared:
                    # A viewer need not read/respond to the close frame. Give
                    # it a short graceful window, then close the transport in
                    # finally; remote cooperation cannot hold takeover open.
                    try:
                        await asyncio.wait_for(ws.close(code=1000, message=b"Screen disconnected"), 0.25)
                    except asyncio.TimeoutError:
                        pass
            finally:
                # Even repeated cancellation must close both capture transports
                # before acknowledging the observer lease's end to supervisor.
                if writer is not None:
                    writer.transport.abort()
                if ws.prepared and request.transport is not None:
                    request.transport.close()
                if observer_writer is not None:
                    observer_writer.close()
                session.streams.discard(ws)
                session.attaching -= 1
        return ws

    async def shutdown(_app):
        await asyncio.gather(*(ws.close(code=1001) for session in list(gateway._display_sessions.values())
                               for ws in tuple(session.streams)), return_exceptions=True)

    app.router.add_get(STREAM_PATH, stream)
    app.on_shutdown.append(shutdown)
