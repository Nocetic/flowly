"""Authenticated direct display admission and bounded binary RFB transport.

The installed host, profile directory and Unix socket are server-resolved.
Stream credentials travel in headers, not URLs, chat events or access logs.
Input travels in epoch-tagged frames and only through the supervisor's broker.
Legacy raw RFB streams remain viewing-only even when a viewer holds control.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import json
import os
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import WSMsgType, web
from loguru import logger

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.input_service import InputServiceClient
from flowly.desktop_runtime.install_jobs import ScreenInstallJobs
from flowly.desktop_runtime.install_plan import installation_plan, screen_installable
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.lifecycle import ScreenLifecycle
from flowly.desktop_runtime.profile_lifecycle import assert_not_retired, construction_guard
from flowly.desktop_runtime.rfb import RfbClientGate
from flowly.desktop_runtime.status import StatusSequence
from flowly.desktop_runtime.stream_input import ScreenInputQueue, coalesce_pointer_moves
from flowly.desktop_runtime.tickets import StreamScope, StreamTickets
from flowly.desktop_runtime.viewers import ViewerCredentials
from flowly.gateway.auth import extract_request_token, host_origin_allowed, token_matches
from flowly.gateway.display_watch import DisplaySubscriptions
from flowly.gateway.mcp_management import _protected_socket

METHODS = frozenset({"display.capabilities", "display.status", "display.observe", "display.viewer.create", "display.viewer.close",
                     "display.control.acquire", "display.control.release", "display.control.reclaim",
                     "display.subscribe", "display.unsubscribe", "display.run.resume",
                     "display.start", "display.stop", "display.operation.status", "display.doctor", "display.install.plan", "display.install.status", "display.install.apply", "display.install.verify",
                     "display.install.restart"})
INSTALL_METHODS = frozenset({"display.install.status", "display.install.apply", "display.install.verify", "display.install.restart"})
STREAM_PATH = "/api/display/stream"
MAX_FRAME = 256 * 1024
MAX_STREAMS = 4
INPUT_PROTOCOL = "epoch-u64-v1"
# A stream re-verifies its profile, generation and control state this often in
# the background. Pixels and input are only checked inline when that proof is
# older than the lease (a stalled monitor), so a stale stream stops instead of
# continuing unverified. Revocation and takeover arrive immediately through the
# supervisor's observer socket.
CURRENT_CHECK_SECONDS = 0.25
CURRENT_LEASE_SECONDS = 0.5
# An unused running screen is stopped after this long; an agent that needs its
# browser again starts it (host_screen_run). Use is an attached viewer stream,
# a viewer/control request, human control or an agent run/action; a static
# picture without new pixels is not idleness.
IDLE_STOP_SECONDS = 20 * 60
IDLE_CHECK_SECONDS = 60
ACTIVITY_METHODS = frozenset({"display.observe", "display.viewer.create", "display.control.acquire",
                              "display.control.release", "display.control.reclaim", "display.run.resume",
                              "display.start"})


def _supports_input(state: dict | None) -> bool:
    return bool(state and state.get("phase") == "running" and state.get("inputServiceVersion") in (2, 3)
                and state.get("observerServiceVersion") in (2, 3))


class DisplaySession:
    def __init__(self, identity: dict[str, str], directory: Path):
        self.identity = dict(identity)
        self.directory = directory
        self._runtime: ScreenRuntime | None = None
        self._tickets: StreamTickets | None = None
        self._viewers: ViewerCredentials | None = None
        self._status_sequence: StatusSequence | None = None
        self._handoffs: RunHandoffs | None = None
        self.streams: set[web.WebSocketResponse] = set()
        self.attaching = 0
        # Idle tracking, owned by the gateway event loop.
        self.activity = time.monotonic()
        self.idle_mark: tuple[str, int] | None = None
        self.idle_stopping = False

    @property
    def runtime(self) -> ScreenRuntime:
        with construction_guard(self.directory):
            assert_not_retired(self.directory)
            if self._runtime is None:
                self._runtime = ScreenRuntime(self.directory, require_parent=True)
            self._runtime.assert_current_directory()
            return self._runtime

    @property
    def tickets(self) -> StreamTickets:
        with construction_guard(self.directory):
            self.runtime.assert_current_directory()
            if self._tickets is None:
                self._tickets = StreamTickets(self.directory / "admission")
            return self._tickets

    def capabilities(self) -> dict:
        capability = ScreenRuntime.capabilities()
        state = self.runtime.status() if capability["supported"] else None
        return self._capabilities(capability, state)

    def _capabilities(self, capability: dict, state: dict | None) -> dict:
        return {**self.identity, "protocolVersion": 1, **capability,
                "transports": ["direct"] if capability["supported"] else [],
                "view": capability["supported"], "input": _supports_input(state),
                "viewerClose": bool(state and state.get("inputServiceVersion") == 3
                                    and state.get("observerServiceVersion") == 3),
                "lifecycle": capability["supported"], "lifecycleVersion": 1, "installSupported": True, "installRestartVersion": 1,
                # Clients offer a screen only where one runs or can be installed.
                "installable": screen_installable(capability),
                "statusEvents": True, "resumeSupported": True,
                "doctorVersion": 1, "installPlanVersion": 1}

    @property
    def viewers(self) -> ViewerCredentials:
        with construction_guard(self.directory):
            self.runtime.assert_current_directory()
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
        with construction_guard(self.directory):
            self.runtime.assert_current_directory()
            if self._status_sequence is None:
                self._status_sequence = StatusSequence(self.directory)
            return self._status_sequence.snapshot(self._status_snapshot)

    def _status_snapshot(self) -> dict:
        available = ScreenRuntime.capabilities()
        state = self.runtime.status() if available["supported"] else None
        control = self.runtime.authority.snapshot() if available["supported"] else None
        if (state and state["phase"] == "running" and control
                and control["generation"] == state["generation"] and not control["running"]):
            # Authority is fenced before graceful Chromium/Xvnc shutdown. Do
            # not advertise a usable running screen or premature completion.
            state = {**state, "phase": "stopping"}
        capability = self._capabilities(available, state)
        if not capability["supported"]:
            return {**capability, "phase": "unavailable", "generation": None, "control": None}
        return {**capability, "phase": state["phase"] if state else "stopped",
                "generation": state["generation"] if state else None,
                "error": state.get("error") if state else None,
                "control": control, "handoff": self.handoffs.status(), "width": 1440, "height": 900}

    @property
    def handoffs(self) -> RunHandoffs:
        with construction_guard(self.directory):
            self.runtime.assert_current_directory()
            if self._handoffs is None:
                self._handoffs = RunHandoffs(self.directory, self.runtime.authority)
            return self._handoffs

    def resume(self, generation: str, token: str, handoff_id: str, epoch: int, operation: str) -> dict:
        self.running(generation)
        self.authenticate_viewer(generation, token)
        status = self.handoffs.status()
        if status is None or any(status[key] != self.identity[key] for key in ("hostId", "botId", "profileId")):
            raise ScreenError("HANDOFF_CHANGED")
        if status["runtimeGeneration"] != generation:
            raise ScreenError("STALE_GENERATION")
        return {**self.identity, "generation": generation,
                **self.handoffs.request_resume(handoff_id, epoch, operation)}

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
        if state.get("observerServiceVersion") not in (1, 2, 3) or state.get("inputServiceVersion") not in (1, 2, 3):
            raise ScreenError("SCREEN_UPGRADE_REQUIRED")
        if method == "display.viewer.close":
            if state.get("observerServiceVersion") != 3 or state.get("inputServiceVersion") != 3:
                raise ScreenError("SCREEN_UPGRADE_REQUIRED")
            viewer = self.viewers.authenticate(token, self.scope(generation), self.identity["botId"],
                                               close=(operation, epoch))
        else:
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
        active_profile_identity,
        current_profile_name,
        get_flowly_home,
        get_or_create_profile_host_id,
        is_default_home,
        profile_identity,
    )

    profile = params.get("profileId", current_profile_name())
    if not isinstance(profile, str) or not profile or len(profile) > 64:
        raise ScreenError("INVALID_PARAMS")
    if profile == current_profile_name():
        # This process's own profile, including a custom FLOWLY_HOME, which is
        # its own installation with its own identity and directory.
        try:
            _, path, bot_id = active_profile_identity()
        except (ValueError, OSError):
            raise ScreenError("PROFILE_NOT_AVAILABLE") from None
        if path.resolve() != get_flowly_home().resolve():
            raise ScreenError("PROFILE_IDENTITY_CHANGED")
    else:
        # Named profiles belong to the default installation's profile host; a
        # custom home never exposes that other installation's profiles.
        if gateway.profile_host is None or not is_default_home():
            raise ScreenError("PROFILE_NOT_AVAILABLE")
        try:
            path, bot_id = profile_identity(profile)
        except (ValueError, FileNotFoundError):
            raise ScreenError("PROFILE_NOT_AVAILABLE") from None
    host = gateway.profile_host.host_id if gateway.profile_host else get_or_create_profile_host_id()
    identity = {"hostId": host, "profileId": profile, "botId": bot_id, "screenId": "browser"}
    guarded = pinned or "expectedHostId" in params or "expectedBotId" in params
    if guarded and (params.get("expectedHostId") != host or params.get("expectedBotId") != bot_id):
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    key = (profile, bot_id)
    sessions = gateway._display_sessions
    if key not in sessions:
        # Bound stale cached identities after profile deletion/recreation.
        stale = [key for key, session in sessions.items() if not session.streams and not session.attaching]
        for old in stale:
            if old[0] == profile or len(sessions) >= 16:
                sessions.pop(old, None)
        if len(sessions) >= 16:
            raise ScreenError("SCREEN_BUSY")
        sessions[key] = DisplaySession(identity, path / "desktop")
    return sessions[key]


async def _restarts_itself() -> bool:
    """Whether this gateway can restart itself after an installation.

    The same proof display.install.restart requires, so a plan that says yes
    is one whose restart will be scheduled rather than left to the owner.
    """
    if sys.platform != "linux":
        return False
    from flowly.integrations.service_control import DEFAULT_LABEL
    from flowly.integrations.service_ownership import owns_current_gateway

    try:
        return await owns_current_gateway(DEFAULT_LABEL, os.getpid())
    except Exception:
        return False


async def _install_restart(gateway, ws, rpc_id: str, session: DisplaySession, params: dict) -> None:
    """Restart this service to load freshly installed packages, only when idle.

    The exclusive environment lease is taken before the reply and held until
    this process exits, so no agent turn, tracked process, screen runtime or
    installation starts between the idle check and the restart. Only a
    systemd user unit whose main process is exactly this gateway is restarted;
    otherwise the host owner restarts it and verification proves the result.
    """
    target = {key: params[key] for key in ("profileId", "expectedHostId", "expectedBotId")}
    operation, plan = params["operationId"], params["planId"]

    def admit():
        jobs = ScreenInstallJobs(session.identity, lambda: _resolve(gateway, target, pinned=True))
        return jobs.restart_reservation(plan, operation)

    needed, reservation = await asyncio.to_thread(admit)
    result = {"installRestartVersion": 1, **session.identity, "operationId": operation, "planId": plan}
    if not needed:
        await gateway._ws_rpc_reply(ws, rpc_id, {**result, "restart": "not_required"})
        return
    try:
        # Accepted chat work that has not started a turn yet would be lost.
        backlog = getattr(gateway, "_maintenance_backlog", None)
        if not callable(backlog) or backlog() != 0:
            raise ScreenError("INSTALL_BUSY")
        from flowly.integrations.service_control import DEFAULT_LABEL
        from flowly.integrations.service_ownership import owns_current_gateway

        if sys.platform != "linux" or not await owns_current_gateway(DEFAULT_LABEL, os.getpid()):
            await gateway._ws_rpc_reply(ws, rpc_id, {**result, "restart": "manual"})
            return
        await gateway._ws_rpc_reply(ws, rpc_id, {**result, "restart": "scheduled"})
        gateway._install_restart = asyncio.create_task(_restart_holding(gateway, reservation))
        reservation = None
    finally:
        if reservation is not None:
            reservation.__exit__(None, None, None)


async def _restart_holding(gateway, reservation) -> None:
    try:
        await asyncio.sleep(0.5)  # Let the reply flush first.
        from flowly.integrations.service_control import restart_gateway

        outcome = await restart_gateway(health_check_host=gateway.host, health_check_port=gateway.port,
                                        expected_pid=os.getpid())
        if not outcome.ok:
            logger.warning("[Gateway] installation restart was not completed: {}", outcome.detail)
    except Exception:
        logger.exception("[Gateway] installation restart failed")
    finally:
        # Reached only if this process survived: work may resume here again.
        reservation.__exit__(None, None, None)


_stream_workers: ThreadPoolExecutor | None = None
_stream_workers_lock = threading.Lock()


async def _stream_call(function, /, *args, **kwargs):
    """Run a stream's blocking check off the loop, in workers of its own.

    The default executor is shared with agent tools; a few blocking jobs there
    queued every frame and keystroke behind them (hundreds of milliseconds).
    """
    global _stream_workers
    with _stream_workers_lock:
        if _stream_workers is None:
            _stream_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="flowly-screen-stream")
    # Same semantics as asyncio.to_thread, including the caller's context.
    call = functools.partial(contextvars.copy_context().run, function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_stream_workers, call)


def _error(code: str, status: int = 400) -> web.Response:
    return web.json_response({"error": {"code": code}}, status=status, headers={"Cache-Control": "no-store"})


async def handle_rpc(gateway, ws, client_id: str, rpc_id: str, method: str, params) -> None:
    request = getattr(ws, "_req", None)
    if not gateway.display_client_authenticated(client_id, ws):
        await gateway._ws_rpc_error(ws, rpc_id, "UNAUTHORIZED", "An authenticated display connection is required.")
        return
    if request is None or not _protected_socket(request) or not host_origin_allowed(request):
        await gateway._ws_rpc_error(ws, rpc_id, "SECURE_TRANSPORT_REQUIRED", "Use TLS or an SSH tunnel for the screen.")
        return
    try:
        if method not in METHODS:
            raise ScreenError("METHOD_NOT_ALLOWED")
        if method == "display.unsubscribe":
            if not isinstance(params, dict) or set(params) != {"subscriptionId"}:
                raise ScreenError("INVALID_PARAMS")
            await gateway._display_subscriptions.remove(ws, params["subscriptionId"])
            await gateway._ws_rpc_reply(ws, rpc_id, {"unsubscribed": True})
            return
        allowed = {"profileId", "expectedHostId", "expectedBotId"}
        pinned = method not in {"display.capabilities", "display.status"}
        if pinned and method not in {"display.subscribe", "display.operation.status", "display.doctor", "display.install.plan"} | INSTALL_METHODS:
            allowed.add("generation")
        if method in {"display.start", "display.stop", "display.operation.status"} | INSTALL_METHODS:
            allowed.add("operationId")
        if method in {"display.install.apply", "display.install.verify", "display.install.restart"}:
            allowed.add("planId")
        if method == "display.install.apply":
            allowed.add("password")
        if method == "display.observe":
            allowed.add("viewerToken")
        if method == "display.run.resume":
            allowed.add("handoffId")
        if method.startswith("display.control.") or method in {"display.viewer.close", "display.run.resume"}:
            allowed.update({"viewerToken", "epoch", "operationId"})
        if not isinstance(params, dict) or set(params) - allowed:
            raise ScreenError("INVALID_PARAMS")
        if method in {"display.install.apply", "display.install.verify", "display.install.restart"} and not {
                "profileId", "expectedHostId", "expectedBotId", "planId", "operationId"}.issubset(params):
            raise ScreenError("INVALID_PARAMS")
        session = await asyncio.to_thread(_resolve, gateway, params, pinned=pinned)
        if method in ACTIVITY_METHODS:
            session.activity = time.monotonic()
        if method == "display.install.plan":
            result = await asyncio.to_thread(installation_plan, session.identity)
            await asyncio.to_thread(_resolve, gateway, params, pinned=True)
            await gateway._ws_rpc_reply(ws, rpc_id, {**result, "selfRestart": await _restarts_itself()})
            return
        if method == "display.install.apply":
            # Retain only target identity in the worker's guard. Never capture
            # the request object (which may contain an ephemeral sudo secret).
            target = {key: params[key] for key in ("profileId", "expectedHostId", "expectedBotId")}
            def apply_installation():
                jobs = ScreenInstallJobs(session.identity, lambda: _resolve(gateway, target, pinned=True))
                return jobs.submit(params.get("planId"), params.get("operationId"), password=params.get("password"))
            result = await asyncio.to_thread(apply_installation)
            await gateway._ws_rpc_reply(ws, rpc_id, result)
            return
        if method == "display.install.restart":
            await _install_restart(gateway, ws, rpc_id, session, params)
            return
        if method in {"display.install.status", "display.install.verify"}:
            def installation_status():
                jobs = ScreenInstallJobs(session.identity, lambda: _resolve(gateway, params, pinned=True))
                result = (jobs.verify(params.get("planId"), params.get("operationId"))
                          if method == "display.install.verify" else jobs.status(params.get("operationId")))
                _resolve(gateway, params, pinned=True)
                return result
            result = await asyncio.to_thread(installation_status)
            await gateway._ws_rpc_reply(ws, rpc_id, result)
            return
        if method == "display.doctor":
            result = await asyncio.to_thread(ScreenRuntime.doctor, session.directory)
            # Profile deletion/recreation during the read must not publish a
            # diagnostic under a stale identity. No runtime is constructed here.
            await asyncio.to_thread(_resolve, gateway, params, pinned=True)
            await gateway._ws_rpc_reply(ws, rpc_id, {**session.identity, **result})
            return
        if method in {"display.start", "display.stop", "display.operation.status"}:
            def lifecycle():
                capability = ScreenRuntime.capabilities()
                if method != "display.operation.status" and not capability["supported"]:
                    raise ScreenError(capability["reason"])
                def guard():
                    _resolve(gateway, params, pinned=True)
                operations = ScreenLifecycle(session.runtime, session.identity, guard)
                if method == "display.operation.status":
                    return operations.status(params.get("operationId"))
                if "generation" not in params:
                    raise ScreenError("INVALID_PARAMS")
                return operations.submit(method.removeprefix("display."), params["generation"], params.get("operationId"))
            result = await asyncio.to_thread(lifecycle)
            await gateway._ws_rpc_reply(ws, rpc_id, result)
            return
        if method == "display.subscribe":
            entry = await gateway._display_subscriptions.prepare(ws, client_id, params)
            try:
                await gateway._ws_rpc_reply(ws, rpc_id, {"subscriptionId": entry.identifier, "status": entry.snapshot})
                gateway._display_subscriptions.activate(entry)
            except BaseException:
                await gateway._display_subscriptions.remove(ws, entry.identifier)
                raise
            return
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
            elif method == "display.run.resume":
                result = await asyncio.to_thread(session.resume, generation, params.get("viewerToken"),
                    params.get("handoffId"), params.get("epoch"), params.get("operationId"))
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
    gateway._display_subscriptions = DisplaySubscriptions(gateway, _resolve)

    async def stream(request: web.Request) -> web.StreamResponse:
        # The ordinary gateway middleware checks Authorization under enforced
        # auth. An exempt loopback gateway still requires the token here: the
        # screen is never reachable without it, even with a ticket.
        if not gateway._auth_token or (not gateway._require_auth and not token_matches(
                extract_request_token(request), gateway._auth_token)):
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
        if session.attaching >= MAX_STREAMS or session.idle_stopping:
            return _error("SCREEN_BUSY", 429)
        session.attaching += 1
        session.activity = time.monotonic()
        writer = None
        observer_writer = None
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME + (8 if input_protocol else 0), heartbeat=15,
                                   compress=False, timeout=2)
        tasks = []
        stream_id = secrets.token_urlsafe(24)
        try:
            if state.get("observerServiceVersion") in (1, 2, 3):
                async with asyncio.timeout(3):
                    observer_reader, observer_writer = await asyncio.open_unix_connection(
                        session.runtime.socket_directory / "observers.sock", limit=4096)
                    registration = {"viewer": viewer, "generation": generation}
                    if state["observerServiceVersion"] in (2, 3):
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
            shared_capture = state.get("captureServiceVersion") == 1
            socket = (session.runtime.socket_directory / "captures.sock" if shared_capture
                      else session.runtime.rfb_socket_for(state))
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(socket, limit=128 * 1024), 5)
            if shared_capture:
                async with asyncio.timeout(3):
                    hello = json.dumps({"viewer": viewer, "generation": generation, "stream": stream_id}).encode()
                    writer.write(len(hello).to_bytes(4, "big") + hello)
                    await writer.drain()
                    size = int.from_bytes(await reader.readexactly(4), "big")
                    if not 0 < size <= 4096 or json.loads(await reader.readexactly(size)) != {"ok": True}:
                        raise ScreenError("CONTROL_UNAVAILABLE")
            writer.transport.set_write_buffer_limits(high=128 * 1024, low=32 * 1024)
            await ws.prepare(request)
            session.streams.add(ws)
            gate = RfbClientGate()
            verified = time.monotonic()

            async def check_current():
                nonlocal verified
                started = time.monotonic()
                fresh = await _stream_call(_resolve, gateway, params, pinned=True)
                if fresh is not session:
                    raise ScreenError("PROFILE_IDENTITY_CHANGED")
                await _stream_call(session.current, generation, viewer)
                verified = max(verified, started)

            async def still_current():
                # Never disk reads per frame or keystroke: the monitor proves
                # this every CURRENT_CHECK_SECONDS; only a stale proof is redone.
                if time.monotonic() - verified > CURRENT_LEASE_SECONDS:
                    await check_current()

            async def server_to_viewer():
                while data := await reader.read(64 * 1024):
                    await still_current()
                    await asyncio.wait_for(ws.send_bytes(data), 5)

            inbox = ScreenInputQueue(bool(input_protocol))

            async def receive_viewer():
                # aiohttp handles ping/pong inside receive(). Never wait here
                # for filesystem locks or input ACKs: they otherwise starve
                # heartbeats behind mouse traffic and kill a healthy stream.
                async for message in ws:
                    if message.type != WSMsgType.BINARY:
                        raise ScreenError("INVALID_RFB_STREAM")
                    inbox.put(message.data)

            async def viewer_to_server():
                input_client = InputServiceClient(session.runtime.socket_directory / "input.sock")
                input_state = None
                input_epoch = None
                while True:
                    epoch, payload = await inbox.get()
                    await still_current()
                    inputs = []
                    allowed = bool(input_protocol and epoch and await _stream_call(
                        session.runtime.authority.may_input, viewer, generation, epoch))
                    data = gate.feed(payload, lambda: allowed, epoch=epoch,
                                     on_input=inputs.append if input_protocol else None)
                    if data:
                        writer.write(data)
                        await asyncio.wait_for(writer.drain(), 5)
                    if inputs:
                        if input_epoch != epoch:
                            input_state = await _stream_call(
                                input_client.request, "attach.capture" if shared_capture else "attach", viewer=viewer, generation=generation,
                                epoch=epoch, stream=stream_id,
                            )
                            input_epoch = epoch
                        batch = bytearray()
                        for item in [*coalesce_pointer_moves(inputs), b""]:
                            if batch and (not item or len(batch) + len(item) > 64 * 1024 + 8):
                                result = await _stream_call(
                                    input_client.request, "send", viewer=viewer, generation=generation, epoch=epoch,
                                    handle=input_state["handle"], sequence=input_state["nextSequence"],
                                    data=base64.b64encode(batch).decode(),
                                )
                                input_state["nextSequence"] = result["nextSequence"]
                                batch.clear()
                            batch.extend(item)

            async def monitor():
                while True:
                    await asyncio.sleep(CURRENT_CHECK_SECONDS)
                    await check_current()
                    if not await _stream_call(session.viewers.active, viewer, session.scope(generation),
                                              session.identity["botId"]):
                        raise ScreenError("VIEWER_EXPIRED")

            tasks.extend(asyncio.create_task(operation()) for operation in (server_to_viewer, receive_viewer, viewer_to_server, monitor))
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
                session.activity = time.monotonic()
        return ws

    async def idle_monitor():
        while True:
            await asyncio.sleep(IDLE_CHECK_SECONDS)
            try:
                await idle_sweep(gateway)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Screen idle check failed")

    async def start_idle_monitor(_app):
        gateway._display_idle_task = asyncio.create_task(idle_monitor())

    async def stop_idle_monitor(_app):
        task = getattr(gateway, "_display_idle_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(_app):
        await stop_idle_monitor(_app)
        await gateway._display_subscriptions.close()
        await asyncio.gather(*(ws.close(code=1001) for session in list(gateway._display_sessions.values())
                               for ws in tuple(session.streams)), return_exceptions=True)

    app.router.add_get(STREAM_PATH, stream)
    app.on_startup.append(start_idle_monitor)
    app.on_shutdown.append(shutdown)


def _screens_on_disk(gateway) -> list[str]:
    """Profiles this gateway serves whose screen has a runtime manifest."""
    from flowly.profile import (
        _PROFILE_NAME_RE,
        _PROFILES_ROOT,
        current_profile_name,
        get_flowly_home,
        is_default_home,
    )

    found = []
    if (get_flowly_home() / "desktop" / "runtime.json").is_file():
        found.append(current_profile_name())
    if gateway.profile_host is not None and is_default_home():
        try:
            entries = sorted(_PROFILES_ROOT.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if (_PROFILE_NAME_RE.match(entry.name) and entry.name not in found
                    and (entry / "desktop" / "runtime.json").is_file()):
                found.append(entry.name)
    return found


def _idle_candidate(session: DisplaySession) -> tuple[str, int] | None:
    """(generation, authority revision) of a running screen nobody uses."""
    if not ScreenRuntime.capabilities()["supported"]:
        return None
    state = session.runtime.status()
    control = session.runtime.authority.snapshot()
    if (not state or state["phase"] != "running" or not control["running"]
            or control["generation"] != state["generation"] or control["control"] != "agent"
            or control["runId"] or control["actionInFlight"] or control["inputDrainRequired"]):
        return None
    return control["generation"], control["revision"]


async def idle_sweep(gateway, now: float | None = None) -> list[str]:
    """Stop running screens unused for IDLE_STOP_SECONDS; returns stopped generations.

    The decision and the stream-admission fence happen on the event loop, so
    no viewer attaches between them; the authority refuses the stop under its
    lock if any control/run change happened since the observed revision.
    Human control or an agent run is never idle, and a dead runtime is left
    for explicit recovery.
    """
    sessions = gateway._display_sessions
    # After a gateway restart nobody may have asked about a screen yet: every
    # profile screen this gateway serves that has a runtime on disk is covered.
    known = {identity[0] for identity in sessions}
    for profile in await asyncio.to_thread(_screens_on_disk, gateway):
        if profile not in known:
            try:
                await asyncio.to_thread(_resolve, gateway, {"profileId": profile})
            except (ScreenError, OSError):
                pass
    stopped = []
    for session in list(sessions.values()):
        current = time.monotonic() if now is None else now
        if session.streams or session.attaching or session.idle_stopping:
            session.activity = current
            continue
        try:
            candidate = await asyncio.to_thread(_idle_candidate, session)
        except (ScreenError, OSError):
            continue
        if candidate is None:
            session.idle_mark = None
            continue
        if session.idle_mark != candidate:
            # Any authority change (control, run, action) restarts the clock.
            session.idle_mark = candidate
            session.activity = max(session.activity, current)
            continue
        if current - session.activity < IDLE_STOP_SECONDS:
            continue
        if session.streams or session.attaching or session.idle_stopping:
            continue
        session.idle_stopping = True
        generation, revision = candidate
        identity = session.identity
        pinned = {"profileId": identity["profileId"], "expectedHostId": identity["hostId"],
                  "expectedBotId": identity["botId"]}

        def guard():
            _resolve(gateway, pinned, pinned=True)

        try:
            await asyncio.to_thread(session.runtime.stop, generation, identity_guard=guard,
                                    idle_revision=revision)
            stopped.append(generation)
            logger.info("Stopped an unused screen for profile {}", identity["profileId"])
        except ScreenError as exc:
            logger.info("Idle screen stop refused: {}", exc.code)
        finally:
            session.idle_stopping = False
            session.idle_mark = None
            session.activity = time.monotonic() if now is None else now
    return stopped
