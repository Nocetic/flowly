"""MCP client core — dedicated event loop, per-server tasks, discovery.

Architecture
------------

A single daemon thread runs a dedicated asyncio event loop
(:data:`_loop`). Every MCP server lives as one long-lived task on that
loop. The task drives the entire transport + session lifecycle inside
a single ``async with`` chain so that the anyio cancel-scopes created
by the MCP SDK's transport clients enter and exit in the same task —
the SDK requires this.

Tool calls from the agent (which runs in some *other* event loop)
reach the MCP loop via :func:`asyncio.run_coroutine_threadsafe`; see
:mod:`flowly.mcp.tool`.

Faz 1 transports: stdio and HTTP (StreamableHTTP). SSE is Faz 2.

Public API
----------

* :func:`discover_mcp_tools` — main entry point called from the agent
  loop at boot.
* :func:`shutdown_mcp_servers` — best-effort graceful teardown.
* :func:`get_mcp_loop` — accessor used by the MCPTool wrapper.

Per-server failure isolation
----------------------------

A failed connect on server *A* must not block server *B*'s discovery
or the agent boot itself. We gather server connects with
``return_exceptions=True`` and only log the exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from flowly.mcp.lifecycle import (
    MCPConnectionState,
    MCPContractChangedError,
    MCPRetryPolicy,
    MCPUnavailableError,
)
from flowly.mcp.media_cache import DEFAULT_MAX_BINARY_BYTES
from flowly.mcp.pagination import MCPPageCollection, collect_mcp_pages
from flowly.mcp.schema import sanitize_mcp_name_component
from flowly.mcp.security import (
    build_safe_env,
    diagnostic_secrets,
    exception_diagnostic,
    interpolate_env_vars,
    sanitize_error,
    scan_description,
)
from flowly.mcp.stderr_log import (
    get_stderr_log,
    read_stderr_excerpt,
    summarize_stderr_excerpt,
    write_stderr_log_header,
)
from flowly.mcp.stdio_resolver import resolve_stdio_command

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional MCP SDK import
# ---------------------------------------------------------------------------

_MCP_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False
_MCP_SSE_AVAILABLE = False
_MCP_NOTIFICATIONS = False
_MCP_MESSAGE_HANDLER = False
_MCP_UNIFIED_CLIENT = False
try:
    from mcp import Client, ClientSession, StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client  # type: ignore

    _MCP_AVAILABLE = True
    _MCP_UNIFIED_CLIENT = True
    try:
        from mcp.client.streamable_http import streamable_http_client  # type: ignore

        _MCP_HTTP_AVAILABLE = True
    except ImportError:
        streamable_http_client = None  # type: ignore
        _MCP_HTTP_AVAILABLE = False
    # SSE transport (T3) — older HTTP-style servers. Optional.
    try:
        from mcp.client.sse import sse_client  # type: ignore

        _MCP_SSE_AVAILABLE = True
    except ImportError:
        sse_client = None  # type: ignore
        _MCP_SSE_AVAILABLE = False
    # Notification types power tools/list_changed hot reload (D8). Older
    # SDKs may not export them; we degrade to static discovery.
    try:
        from mcp.types import (  # type: ignore
            ServerNotification,
            ToolListChangedNotification,
        )

        _MCP_NOTIFICATIONS = True
    except ImportError:
        logger.debug("MCP notification types unavailable — list_changed disabled")
    # ClientSession only accepts ``message_handler`` on newer SDKs.
    try:
        import inspect as _inspect

        _MCP_MESSAGE_HANDLER = "message_handler" in _inspect.signature(ClientSession).parameters
    except (TypeError, ValueError):
        _MCP_MESSAGE_HANDLER = False
except ImportError:
    Client = None  # type: ignore
    logger.debug("mcp SDK not installed — MCP discovery disabled")


# ---------------------------------------------------------------------------
# Background event loop singleton
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()
_servers: dict[str, "MCPServerTask"] = {}
_servers_lock = threading.RLock()
_shutting_down = False


@dataclass
class _Startup:
    server: Any
    identity: str
    task: asyncio.Task | None = None
    waiters: int = 0


_starting: dict[str, _Startup] = {}


# ---------------------------------------------------------------------------
# Circuit breaker (T10)
# ---------------------------------------------------------------------------
#
# After a server racks up N consecutive failed tool calls we "open" the
# breaker: further calls short-circuit with a clear message so the model
# stops hammering a dead server and tries another approach. After the
# cooldown elapses the breaker is half-open — the next call goes through
# as a probe; success resets it, failure re-arms the cooldown.

_CIRCUIT_BREAKER_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0

_breaker_lock = threading.Lock()
_server_error_counts: dict[str, int] = {}
_server_breaker_opened_at: dict[str, float] = {}
_server_breaker_probe_inflight: set[str] = set()

def _bump_server_error(server_name: str) -> None:
    import time

    with _breaker_lock:
        _server_breaker_probe_inflight.discard(server_name)
        count = _server_error_counts.get(server_name, 0) + 1
        _server_error_counts[server_name] = count
        if count >= _CIRCUIT_BREAKER_THRESHOLD:
            _server_breaker_opened_at[server_name] = time.monotonic()


def _reset_server_error(server_name: str) -> None:
    with _breaker_lock:
        _server_breaker_probe_inflight.discard(server_name)
        _server_error_counts.pop(server_name, None)
        _server_breaker_opened_at.pop(server_name, None)


def _release_server_probe(server_name: str) -> None:
    """Release a half-open probe without changing breaker accounting."""
    with _breaker_lock:
        _server_breaker_probe_inflight.discard(server_name)


def circuit_breaker_block_reason(server_name: str) -> str | None:
    """Return a user-facing message if the breaker is open, else ``None``.

    When the cooldown has elapsed we return ``None`` (half-open) so the
    caller lets one probe through; the call's own success/failure path
    then resets or re-arms the breaker.
    """
    import time

    with _breaker_lock:
        count = _server_error_counts.get(server_name, 0)
        if count < _CIRCUIT_BREAKER_THRESHOLD:
            return None
        opened_at = _server_breaker_opened_at.get(server_name, 0.0)
        age = time.monotonic() - opened_at
        if age >= _CIRCUIT_BREAKER_COOLDOWN_SEC:
            if server_name in _server_breaker_probe_inflight:
                return (
                    f"MCP server '{server_name}' recovery probe is already in progress. "
                    "Do NOT retry this tool yet."
                )
            _server_breaker_probe_inflight.add(server_name)
            return None  # half-open: allow exactly one probe
        remaining = max(1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - age))
    return (
        f"MCP server '{server_name}' is unreachable after {count} consecutive "
        f"failures. Auto-retry available in ~{remaining}s. Do NOT retry this "
        f"tool yet — use a different approach or ask the user to check the "
        f"MCP server."
    )


class MCPCallInterrupted(Exception):
    """Raised when a user interrupt cancels an in-flight MCP call."""


def get_mcp_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running MCP background loop, or ``None`` if not started."""
    return _loop


def get_mcp_server_health() -> dict[str, dict[str, Any]]:
    """Return runtime health for configured servers connected in this process."""
    with _servers_lock:
        servers = list(_servers.items())
    return {name: server.health_snapshot() for name, server in servers}


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start the MCP background event loop if not already running."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        if _loop_thread is not None and _loop_thread.is_alive():
            _loop_thread.join(timeout=5)
            if _loop_thread.is_alive():
                raise MCPUnavailableError("Previous MCP loop is still stopping")

        ready = threading.Event()
        loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _runner() -> None:
            global _shutting_down
            loop = asyncio.new_event_loop()
            loop_holder["loop"] = loop
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)  # Signal only once run_forever is actually running.
            try:
                loop.run_forever()
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.wait(pending, timeout=2))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()
                except Exception:
                    pass
                finally:
                    with _servers_lock:
                        _shutting_down = False

        thread = threading.Thread(target=_runner, name="flowly-mcp-loop", daemon=True)
        thread.start()
        ready.wait()
        _loop = loop_holder["loop"]
        _loop_thread = thread
        return _loop


def _stop_loop(
    *, expected_loop: asyncio.AbstractEventLoop | None = None, request_stop: bool = True,
) -> None:
    """Stop the MCP background loop (best effort). Used by shutdown."""
    global _loop, _loop_thread
    with _loop_lock:
        loop = _loop
        if loop is None or (expected_loop is not None and loop is not expected_loop):
            return
        if request_stop:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if _loop_thread is not None and _loop_thread is not threading.current_thread():
            _loop_thread.join(timeout=5)
        if _loop_thread is None or not _loop_thread.is_alive():
            _loop = None
            _loop_thread = None


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


class InvalidMCPUrlError(ValueError):
    """Raised when a remote MCP server URL is not parseable as http(s)://."""


def _validate_http_url(server_name: str, url: Any) -> str:
    if not isinstance(url, str):
        raise InvalidMCPUrlError(
            f"MCP server '{server_name}': url must be a string, got {type(url).__name__}"
        )
    stripped = url.strip()
    if not stripped:
        raise InvalidMCPUrlError(f"MCP server '{server_name}': empty url")
    try:
        parsed = urlparse(stripped)
    except Exception as exc:
        raise InvalidMCPUrlError(f"MCP server '{server_name}': invalid url ({exc})") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidMCPUrlError(
            f"MCP server '{server_name}': scheme must be http or https, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise InvalidMCPUrlError(f"MCP server '{server_name}': missing host in URL")
    return stripped


# ---------------------------------------------------------------------------
# MCPServerTask
# ---------------------------------------------------------------------------


class MCPServerTask:
    """One MCP server, one asyncio task, one transport context.

    Lifecycle (on the MCP loop):

    1. ``start(config)`` schedules ``_run`` as a task.
    2. ``_run`` opens the transport + session, calls ``initialize``,
       fetches the tool list, signals readiness via ``ready`` event.
    3. ``_run`` blocks on ``shutdown_event`` until torn down.
    4. ``shutdown()`` sets ``shutdown_event`` from any thread.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.session: Any | None = None
        self.tools: list[Any] = []
        self.tool_timeout: float = 120.0
        self.connect_timeout: float = 60.0
        self.max_binary_bytes: int = DEFAULT_MAX_BINARY_BYTES
        self.pagination_max_pages: int = 100
        self.pagination_max_items: int = 10_000
        self.supports_parallel_tool_calls: bool = False
        self.max_parallel_tool_calls: int = 8
        self.capabilities: Any | None = None
        # When True, an HTTP server configured for OAuth may launch the
        # interactive browser flow. False (agent boot) restricts OAuth to
        # silently using stored/refreshable tokens.
        self.interactive: bool = False
        self._config: dict[str, Any] = {}
        self._interaction: Any | None = None
        self._task: asyncio.Task[Any] | None = None
        # Asyncio primitives MUST be created on the loop they belong to.
        # We allocate them lazily in ``_run`` when the loop is known.
        self.ready: asyncio.Event | None = None
        self.shutdown_event: asyncio.Event | None = None
        self.rpc_lock: asyncio.Lock | None = None
        self.tool_call_semaphore: asyncio.Semaphore | None = None
        self.error: BaseException | None = None
        # Set by the discovery layer so dynamic tools/list_changed
        # refreshes (D8) can re-register into the live registry.
        self._registry: Any | None = None
        self._server_cfg: dict[str, Any] = {}
        self._registered_names: list[str] = []
        self._registry_bindings: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._registry_lock = threading.RLock()
        self._refresh_lock: asyncio.Lock | None = None
        self._pending_refreshes: set[asyncio.Task[Any]] = set()
        self.connection_failed_event: asyncio.Event | None = None
        self._connection_failure: BaseException | None = None
        self._retry_policy = MCPRetryPolicy()
        self._ever_connected = False
        self._connection_generation = 0
        self._connected_monotonic: float | None = None
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self._last_error = ""
        self._last_failure_at: float | None = None
        self._state_changed_at = time.time()
        self._protocol_version = ""
        self._protocol_era = ""
        self._tool_pagination = MCPPageCollection(items=(), pages=0)
        self._inflight_tool_calls = 0
        self._peak_inflight_tool_calls = 0
        self._active_requests = 0
        self._waiting_requests = 0
        self._last_activity = time.monotonic()
        self._activity_event: asyncio.Event | None = None
        self._state_event: asyncio.Event | None = None
        self._wake_event: asyncio.Event | None = None
        self._connect_deadline: asyncio.Timeout | None = None
        self._planned_recycle = False
        self._recycle_count = 0
        self._profile_home: Any | None = None
        self._config_identity = ""
        self._manifest_store: Any | None = None
        self._from_manifest = False
        self._catalog_source = "none"
        self.state = MCPConnectionState.IDLE

    def is_http(self) -> bool:
        return bool(self._config.get("url"))

    def bind_registry(self, registry: Any, server_cfg: dict[str, Any]) -> None:
        """Record the registry + config used for dynamic re-registration."""
        try:
            self._registry_bindings.setdefault(registry, (server_cfg, []))
        except TypeError:
            pass  # Non-weakrefable custom registries retain the single binding.
        self._registry = registry
        self._server_cfg = server_cfg

    def set_registered_names(self, names: list[str]) -> None:
        self._registered_names = list(names)
        try:
            self._registry_bindings[self._registry] = (self._server_cfg, list(names))
        except TypeError:
            pass

    def _set_state(self, state: MCPConnectionState) -> None:
        if self.state is state:
            return
        self.state = state
        self._state_changed_at = time.time()
        if self._state_event is not None:
            self._state_event.set()
            self._state_event = asyncio.Event()

    def _mark_connected(self, session: Any | None = None) -> None:
        """Record a completed handshake and wake the initial caller."""
        if self._ever_connected:
            self._reconnect_count += 1
        self._ever_connected = True
        self._connection_generation += 1
        self._connected_monotonic = time.monotonic()
        self._last_activity = self._connected_monotonic
        if self._connect_deadline is not None:
            self._connect_deadline.reschedule(None)
        self._consecutive_failures = 0
        if session is not None:
            self._protocol_version = str(getattr(session, "protocol_version", "") or "")
            self._protocol_era = (
                "modern" if getattr(session, "discover_result", None) is not None else "legacy"
            )
        self._set_state(MCPConnectionState.CONNECTED)
        _reset_server_error(self.name)
        if self.ready is not None:
            self.ready.set()

    def health_snapshot(self) -> dict[str, Any]:
        """Return a stable, credential-free lifecycle snapshot for UIs."""
        return {
            "name": self.name,
            "state": self.state.value,
            "connected": self.session is not None and self.state is MCPConnectionState.CONNECTED,
            "reconnectCount": self._reconnect_count,
            "consecutiveFailures": self._consecutive_failures,
            "lastError": self._last_error,
            "lastFailureAt": self._last_failure_at,
            "stateChangedAt": self._state_changed_at,
            "protocolMode": self._protocol_mode() if self._config else "auto",
            "protocolEra": self._protocol_era,
            "protocolVersion": self._protocol_version,
            "discoveredToolCount": len(self.tools),
            "toolPagination": self._tool_pagination.metadata(),
            "parallelToolCalls": self.supports_parallel_tool_calls,
            "maxParallelToolCalls": (
                self.max_parallel_tool_calls if self.supports_parallel_tool_calls else 1
            ),
            "inflightToolCalls": self._inflight_tool_calls,
            "peakInflightToolCalls": self._peak_inflight_tool_calls,
            "activeRequests": self._active_requests,
            "waitingRequests": self._waiting_requests,
            "recycleCount": self._recycle_count,
            "idleTimeout": self._retry_policy.idle_timeout,
            "maxLifetime": self._retry_policy.max_lifetime,
            "catalogSource": self._catalog_source,
        }

    def report_transport_failure(
        self,
        exc: BaseException,
        failed_session: Any | None = None,
    ) -> None:
        """Ask the supervisor to recycle the currently active session.

        ``failed_session`` prevents a late exception from an old request from
        tearing down a freshly reconnected session.
        """
        if failed_session is not None and failed_session is not self.session:
            return
        self._connection_failure = exc
        if self.connection_failed_event is not None:
            self.connection_failed_event.set()

    async def start(
        self, config: dict[str, Any], *, use_manifest: bool = False, expected_identity: str | None = None,
    ) -> None:
        """Spawn the run-task on the current loop and wait for readiness."""
        if self._task is not None:
            raise RuntimeError(f"MCP server '{self.name}' already started")
        self._config = config
        self._retry_policy = MCPRetryPolicy.from_server_config(config)
        self.tool_timeout = float(config.get("timeout", 120.0))
        self.connect_timeout = float(config.get("connect_timeout", 60.0))
        content_cfg = config.get("content") or {}
        pagination_cfg = config.get("pagination") or {}
        self.max_binary_bytes = int(
            content_cfg.get("max_binary_bytes", DEFAULT_MAX_BINARY_BYTES)
        )
        self.pagination_max_pages = int(pagination_cfg.get("max_pages", 100))
        self.pagination_max_items = int(pagination_cfg.get("max_items", 10_000))
        self.supports_parallel_tool_calls = bool(
            config.get("supports_parallel_tool_calls", False)
        )
        self.max_parallel_tool_calls = int(config.get("max_parallel_tool_calls", 8))
        if not 1 <= self.max_parallel_tool_calls <= 256:
            raise ValueError(
                f"MCP server '{self.name}': max_parallel_tool_calls must be between 1 and 256"
            )
        if not math.isfinite(self.tool_timeout) or self.tool_timeout <= 0:
            raise ValueError(f"MCP server '{self.name}': timeout must be a positive finite number")
        if not math.isfinite(self.connect_timeout) or self.connect_timeout <= 0:
            raise ValueError(
                f"MCP server '{self.name}': connect_timeout must be a positive finite number"
            )
        self.ready = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self.connection_failed_event = asyncio.Event()
        self.rpc_lock = asyncio.Lock()
        self.tool_call_semaphore = asyncio.Semaphore(self.max_parallel_tool_calls)
        self._refresh_lock = asyncio.Lock()
        self._activity_event = asyncio.Event()
        self._state_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self.error = None
        from flowly.mcp.manifest import ManifestStore, configuration_fingerprint
        from flowly.profile import get_flowly_home

        self._profile_home = get_flowly_home().expanduser().resolve()
        self._config_identity = configuration_fingerprint(self.name, config, self._profile_home)
        if expected_identity is not None and expected_identity != self._config_identity:
            raise MCPUnavailableError("MCP configuration context changed before startup")
        if use_manifest and self._retry_policy.lazy_start and not self.interactive:
            self._manifest_store = ManifestStore(
                self._profile_home, self.name, self._config_identity,
                self._retry_policy.manifest_ttl, oauth=config.get("auth") == "oauth",
            )
            cached = await asyncio.to_thread(self._manifest_store.load)
            if cached is not None:
                self.tools = list(cached.tools)
                self.capabilities = cached.capabilities
                self._tool_pagination = MCPPageCollection(items=cached.tools, pages=0)
                self._from_manifest = True
                self._catalog_source = "manifest"
                self.ready.set()
                self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
                return
        self._set_state(MCPConnectionState.CONNECTING)

        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")

        # Wait for whichever fires first: readiness or the run-task
        # exiting (typically with an error). ``shield`` lets us put the
        # run-task in the wait set without cancelling it when we stop
        # waiting — we still need it alive on the happy path.
        ready_wait = asyncio.create_task(self.ready.wait())
        task_view = asyncio.shield(self._task)
        try:
            await asyncio.wait(
                {ready_wait, task_view},
                timeout=self.connect_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            await self._cancel_run_task()
            raise
        finally:
            if not ready_wait.done():
                ready_wait.cancel()
                try:
                    await ready_wait
                except asyncio.CancelledError:
                    pass

        if self.ready.is_set():
            return

        # The run-task records transport failures on ``self.error`` so it can
        # log and exit cleanly.  A clean task result therefore does NOT mean
        # the connection succeeded: surface the recorded root cause instead
        # of mislabelling every fast subprocess/OAuth failure as a timeout.
        if self._task.done():
            exc = self._task.exception()
            if exc is not None:
                raise exc
            if self.error is not None:
                raise self.error
            raise RuntimeError(f"MCP server '{self.name}' exited before initialization")

        # A real timeout: cancel AND await the in-flight task so transport
        # context managers finish closing before this method returns.  Merely
        # calling cancel() leaks subprocesses/tasks under repeated probes.
        await self._cancel_run_task()
        raise asyncio.TimeoutError(
            f"MCP server '{self.name}' connect timed out after {self.connect_timeout:.0f}s"
        )

    @asynccontextmanager
    async def connection_lease(self):
        """Pin a freshly negotiated session for one complete external request.

        Counts tools, resource reads, prompt reads, queueing and elicitation.
        Waiting for a new connection does not pin an old draining session.
        All admission and release decisions run on the owning MCP loop.
        """
        self._waiting_requests += 1
        try:
            async with asyncio.timeout(self.connect_timeout):
                while True:
                    changed = self._state_event
                    if (
                        self.shutdown_event is None or self.shutdown_event.is_set()
                        or self._task is None or self._task.done()
                    ):
                        raise MCPUnavailableError(f"MCP server '{self.name}' is stopped")
                    if self.session is not None and self.state is MCPConnectionState.CONNECTED:
                        session = self.session
                        # No await between admission check and pinning this generation.
                        self._active_requests += 1
                        break
                    if self.state is MCPConnectionState.IDLE and self._wake_event is not None:
                        self._wake_event.set()
                    assert changed is not None
                    await changed.wait()
        except TimeoutError as exc:
            raise MCPUnavailableError(
                f"MCP server '{self.name}' connection was not ready within "
                f"{self.connect_timeout:g}s; no operation was sent"
            ) from exc
        finally:
            self._waiting_requests -= 1
        try:
            yield session
        finally:
            self._active_requests -= 1
            self._last_activity = time.monotonic()
            if self._activity_event is not None:
                self._activity_event.set()

    def validate_tool_contract(self, remote_name: str, expected: str) -> None:
        """Never dispatch an old handler against changed remote semantics."""
        from flowly.mcp.tool import MCPTool

        matching = [tool for tool in self.tools if getattr(tool, "name", "") == remote_name]
        if len(matching) == 1 and _filter_remote_tool(self._config, remote_name):
            current = MCPTool(server_task=self, remote_tool=matching[0])
            if current.contract_fingerprint() == expected:
                return
        raise MCPContractChangedError(
            f"MCP tool '{remote_name}' changed or is no longer available. "
            "Refresh the tool list and review the current schema and permissions; "
            "no operation was sent."
        )

    async def _wait_for_demand(self) -> bool:
        """Keep one supervisor alive, with no transport or child process."""
        assert self._wake_event is not None and self.shutdown_event is not None
        self._set_state(MCPConnectionState.IDLE)
        if self._waiting_requests:
            self._wake_event.set()
        shutdown_wait = asyncio.create_task(self.shutdown_event.wait())
        try:
            while not self.shutdown_event.is_set():
                wake_wait = asyncio.create_task(self._wake_event.wait())
                try:
                    await asyncio.wait(
                        {wake_wait, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    wake_wait.cancel()
                    await asyncio.gather(wake_wait, return_exceptions=True)
                self._wake_event.clear()
                if self.shutdown_event.is_set():
                    return False
                if self._waiting_requests:
                    return True
            return False
        finally:
            shutdown_wait.cancel()
            await asyncio.gather(shutdown_wait, return_exceptions=True)

    async def _recycle_when_due(self) -> None:
        """Stop admitting at lifetime expiry, then let admitted requests drain."""
        assert self._activity_event is not None
        while True:
            self._activity_event.clear()
            now = time.monotonic()
            lifetime = self._retry_policy.max_lifetime
            idle = self._retry_policy.idle_timeout
            lifetime_left = (
                self._connected_monotonic + lifetime - now
                if lifetime and self._connected_monotonic is not None else None
            )
            idle_left = self._last_activity + idle - now if idle else None
            if lifetime_left is not None and lifetime_left <= 0:
                self._set_state(MCPConnectionState.DRAINING)
                if not self._active_requests:
                    return
                await self._activity_event.wait()
                continue
            if not self._active_requests and idle_left is not None and idle_left <= 0:
                self._set_state(MCPConnectionState.DRAINING)
                return
            deadlines = [left for left in (lifetime_left,) if left is not None]
            if not self._active_requests and idle_left is not None:
                deadlines.append(idle_left)
            try:
                await asyncio.wait_for(
                    self._activity_event.wait(), timeout=min(deadlines) if deadlines else None,
                )
            except TimeoutError:
                pass

    @asynccontextmanager
    async def tool_call_slot(self):
        """Apply the server's declared tool-call concurrency contract."""
        if self.rpc_lock is None or self.tool_call_semaphore is None:
            raise RuntimeError(f"MCP server '{self.name}' has not started")
        guard = self.tool_call_semaphore if self.supports_parallel_tool_calls else self.rpc_lock
        async with guard:
            self._inflight_tool_calls += 1
            self._peak_inflight_tool_calls = max(
                self._peak_inflight_tool_calls,
                self._inflight_tool_calls,
            )
            try:
                yield
            finally:
                self._inflight_tool_calls = max(0, self._inflight_tool_calls - 1)

    async def _cancel_run_task(self) -> None:
        """Cancel and join the transport task on its owning event loop."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass

    async def shutdown(self) -> None:
        """Ask the run-task to exit, then wait for it."""
        self._set_state(MCPConnectionState.STOPPING)
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        if self._task is not None:
            if self._task.done():
                try:
                    self._task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                self.session = None
                self._set_state(MCPConnectionState.STOPPED)
                return
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                await self._cancel_run_task()
        self.session = None
        self._set_state(MCPConnectionState.STOPPED)

    async def _run(self) -> None:
        reconnect_attempt = 0
        try:
            if self._from_manifest and not await self._wait_for_demand():
                return
            while self.shutdown_event is not None and not self.shutdown_event.is_set():
                generation_before = self._connection_generation
                if self._ever_connected:
                    self._set_state(self._retry_policy.state_for(max(1, reconnect_attempt)))
                else:
                    self._set_state(MCPConnectionState.CONNECTING)

                try:
                    self._planned_recycle = False
                    self._validate_runtime_identity()
                    # Keep the SDK's transport enter/exit in this same task.
                    # The handshake timer is disabled by _mark_connected;
                    # it also bounds reconnects after the boot waiter is gone.
                    async with asyncio.timeout(self.connect_timeout) as deadline:
                        self._connect_deadline = deadline
                        await self._run_transport()
                    self._connect_deadline = None
                    if self.shutdown_event.is_set():
                        break
                    if self._planned_recycle:
                        self._recycle_count += 1
                        self.session = None
                        if not await self._wait_for_demand():
                            break
                        reconnect_attempt = 0
                        continue
                    if not self._ever_connected:
                        raise RuntimeError(
                            f"MCP server '{self.name}' exited before initialization"
                        )
                    raise ConnectionError("MCP transport exited unexpectedly")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if isinstance(exc, TimeoutError) and (
                        self._connect_deadline is not None and self._connect_deadline.expired()
                    ):
                        phase = "recycle teardown" if self._planned_recycle else "connect"
                        timeout = (
                            self._retry_policy.close_timeout if self._planned_recycle
                            else self.connect_timeout
                        )
                        exc = TimeoutError(
                            f"MCP server '{self.name}' {phase} timed out after {timeout:.0f}s"
                        )
                    self._connect_deadline = None
                    self.session = None
                    self.error = exc
                    self._last_error = exception_diagnostic(exc, secrets=diagnostic_secrets(self._config))
                    self._last_failure_at = time.time()
                    if self._planned_recycle:
                        # Transport context managers have unwound before we
                        # reach this handler. Cleanup failure must not turn
                        # an intentionally idle server into a restart storm.
                        self._recycle_count += 1
                        logger.warning("MCP server '%s' recycle cleanup: %s", self.name, self._last_error)
                        if not await self._wait_for_demand():
                            break
                        reconnect_attempt = 0
                        continue

                    if not self._ever_connected and not self._from_manifest:
                        self._set_state(MCPConnectionState.FAILED)
                        logger.warning(
                            "MCP server '%s' initial connection failed: %s",
                            self.name,
                            self._last_error,
                        )
                        return

                    if not self._retry_policy.reconnect_enabled:
                        self._set_state(MCPConnectionState.FAILED)
                        logger.warning(
                            "MCP server '%s' connection failed; reconnect is disabled: %s",
                            self.name,
                            self._last_error,
                        )
                        return

                    connected_during_attempt = self._connection_generation > generation_before
                    stable_for = (
                        time.monotonic() - self._connected_monotonic
                        if self._connected_monotonic is not None
                        else 0.0
                    )
                    if (
                        connected_during_attempt
                        and stable_for >= self._retry_policy.stable_connection_seconds
                    ):
                        reconnect_attempt = 0
                    reconnect_attempt += 1
                    self._consecutive_failures = reconnect_attempt
                    retry_state = self._retry_policy.state_for(reconnect_attempt)
                    self._set_state(MCPConnectionState.DEGRADED)
                    delay = self._retry_policy.delay_for(reconnect_attempt)
                    action = (
                        "probing parked server"
                        if retry_state is MCPConnectionState.PARKED
                        else "reconnecting"
                    )
                    logger.warning(
                        "MCP server '%s' disconnected: %s; %s in %.1fs",
                        self.name,
                        self._last_error,
                        action,
                        delay,
                    )
                    self._set_state(retry_state)
                    if await self._wait_for_shutdown(delay):
                        break
        except asyncio.CancelledError:
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                self._set_state(MCPConnectionState.STOPPED)
            else:
                self._set_state(MCPConnectionState.FAILED)
            raise
        finally:
            self._connect_deadline = None
            self.session = None
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                self._set_state(MCPConnectionState.STOPPED)

    async def _run_transport(self) -> None:
        """Run one transport lifetime; split out for supervision and tests."""
        if self.is_http():
            await self._run_http()
        else:
            await self._run_stdio()

    def _validate_runtime_identity(self) -> None:
        """Do not reconnect a retained server under a different profile/env."""
        from flowly.mcp.manifest import configuration_fingerprint
        from flowly.profile import get_flowly_home

        home = get_flowly_home().expanduser().resolve()
        if self._config_identity and configuration_fingerprint(self.name, self._config, home) != self._config_identity:
            raise MCPUnavailableError("MCP configuration context changed; reload the configured servers")

    async def _wait_for_shutdown(self, delay: float) -> bool:
        """Sleep interruptibly. Return True when shutdown won the race."""
        assert self.shutdown_event is not None
        try:
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    def get_interaction(self) -> Any:
        if self._interaction is None:
            from flowly.mcp.interaction import MCPInteraction

            self._interaction = MCPInteraction(self.name, self._config)
        return self._interaction

    def _session_kwargs(self) -> dict[str, Any]:
        """Build ClientSession kwargs — list_changed handler + sampling."""
        kwargs: dict[str, Any] = {}
        interaction = self.get_interaction()
        if interaction.enabled:
            kwargs["elicitation_callback"] = interaction.elicit
        if _MCP_NOTIFICATIONS and _MCP_MESSAGE_HANDLER:
            kwargs["message_handler"] = self._make_message_handler()
        # Sampling (Faz 3d): install a callback only when the server opted in.
        sampling_cfg = self._config.get("sampling") or {}
        if sampling_cfg.get("enabled"):
            try:
                from flowly.mcp.sampling import build_sampling_callback

                cb = build_sampling_callback(self.name, sampling_cfg)
                if cb is not None:
                    kwargs["sampling_callback"] = cb
            except Exception as exc:  # pragma: no cover
                logger.debug("MCP sampling callback unavailable: %s", exception_diagnostic(exc, secrets=diagnostic_secrets(self._config)))
        return kwargs

    async def _run_stdio(self) -> None:
        if not (_MCP_AVAILABLE and _MCP_UNIFIED_CLIENT):
            raise ImportError("mcp SDK is not installed")

        command = self._config.get("command") or ""
        if not command:
            raise ValueError(f"MCP server '{self.name}': stdio entry needs 'command'")

        args = list(self._config.get("args") or [])
        user_env = self._config.get("env") or {}
        safe_env = build_safe_env(user_env)
        resolved_command, resolved_env = resolve_stdio_command(command, safe_env)

        # OSV malware gate (S6): block spawn if the npx/uvx package has a
        # known MAL-* advisory. Fail-open; default on, per-server opt-out.
        if self._config.get("osv_check", True):
            from flowly.mcp.osv import check_package_for_malware

            blocked = check_package_for_malware(command, args)
            if blocked:
                raise ValueError(f"MCP server '{self.name}': {blocked}")

        server_params = StdioServerParameters(
            command=resolved_command,
            args=args,
            env=resolved_env if resolved_env else None,
        )

        stderr_offset = write_stderr_log_header(self.name)
        errlog = get_stderr_log()

        # Orphan reap (S7) is OPT-IN per server (reap_orphans). Default
        # off: the spawn-window child diff can, in rare races, attribute
        # an unrelated subprocess (e.g. a concurrent exec-tool bash) to
        # this server, and force-killing the wrong PID is destructive.
        # The MCP SDK already tears the child down on normal exit; this
        # only helps the Linux setsid-escapes-on-cancel edge case.
        reap = bool(self._config.get("reap_orphans"))
        try:
            if not reap:
                await self._run_client_transport(stdio_client(server_params, errlog=errlog))
                return

            from flowly.mcp.proc import reap_pids, snapshot_child_pids

            before = snapshot_child_pids()
            spawned: set[int] = set()

            @asynccontextmanager
            async def _tracked_stdio_transport():
                async with stdio_client(server_params, errlog=errlog) as streams:
                    spawned.update(snapshot_child_pids() - before)
                    yield streams

            try:
                await self._run_client_transport(_tracked_stdio_transport())
            finally:
                # Runs on clean exit, error, and cancellation. If the SDK's
                # own teardown already reaped the child, reap_pids is a no-op.
                reap_pids(spawned, self.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            diagnostic = summarize_stderr_excerpt(read_stderr_excerpt(stderr_offset))
            if diagnostic:
                raise RuntimeError(
                    f"MCP server '{self.name}' stdio transport failed: {diagnostic}"
                ) from exc
            raise

    def _use_sse(self) -> bool:
        """Decide whether to use the SSE transport for this HTTP server."""
        return str(self._config.get("transport") or "auto").lower() == "sse"

    async def _run_http(self) -> None:
        url = _validate_http_url(self.name, self._config.get("url"))
        headers = {
            str(key): str(value)
            for key, value in (self._config.get("headers") or {}).items()
            if str(key).lower() not in {"mcp-protocol-version", "mcp-method", "mcp-name"}
        }

        # OAuth 2.1 / PKCE (Faz 2b): when configured, attach the SDK's
        # OAuthClientProvider as the httpx auth flow. It transparently
        # uses stored tokens, refreshes them, and (interactive only)
        # runs the browser authorization flow on first use.
        auth = None
        if str(self._config.get("auth") or "") == "oauth":
            from flowly.mcp.oauth import build_oauth_provider

            auth = build_oauth_provider(
                self.name,
                url,
                interactive=self.interactive,
                scope=self._config.get("scope") or None,
                allow_same_origin_paths=self._use_sse(),
            )
            if auth is None:
                raise ImportError(
                    "OAuth configured but this 'mcp' SDK build lacks "
                    "mcp.client.auth — upgrade the package."
                )

        # mTLS / custom CA. The 2.x transport accepts an already-configured
        # httpx2 client so auth, headers, and TLS policy share one owner.
        from flowly.mcp.tls import make_http_client_factory, needs_custom_tls

        if self._use_sse():
            client_factory = None
            if needs_custom_tls(self._config):
                client_factory = make_http_client_factory(self.name, self._config)
            await self._run_sse(url, headers, auth, client_factory)
            return

        if not _MCP_HTTP_AVAILABLE:
            raise ImportError(
                "HTTP MCP transport unavailable — upgrade the 'mcp' package "
                "to a version that exports mcp.client.streamable_http."
            )
        client_factory = make_http_client_factory(self.name, self._config)
        http_client = client_factory(headers=headers, timeout=None, auth=auth)
        async with http_client:
            transport = streamable_http_client(url, http_client=http_client)
            await self._run_client_transport(transport)

    async def _run_sse(self, url, headers, auth, client_factory) -> None:
        if not _MCP_SSE_AVAILABLE or sse_client is None:
            raise ImportError(
                "SSE MCP transport unavailable — upgrade the 'mcp' package "
                "to a version that exports mcp.client.sse."
            )
        kwargs: dict[str, Any] = {"headers": headers, "auth": auth}
        if client_factory is not None:
            kwargs["httpx_client_factory"] = client_factory
        await self._run_client_transport(sse_client(url, **kwargs), force_legacy=True)

    def _protocol_mode(self) -> str:
        mode = str(self._config.get("protocol") or "auto").lower()
        if mode not in {"auto", "stateless", "legacy"}:
            raise ValueError(
                f"MCP server '{self.name}': protocol must be auto, stateless, or legacy"
            )
        return mode

    async def _run_client_transport(self, transport: Any, *, force_legacy: bool = False) -> None:
        """Negotiate one SDK transport and serve its connected session."""
        mode = "legacy" if force_legacy else self._protocol_mode()
        session_kwargs = self._session_kwargs()

        if mode == "stateless":
            async with transport as (read, write):
                async with ClientSession(read, write, **session_kwargs) as session:
                    await asyncio.wait_for(session.discover(), timeout=self.connect_timeout)
                    await self._serve_connected(session)
            return

        assert Client is not None
        async with Client(transport, mode=mode, **session_kwargs) as connected:
            await self._serve_connected(connected.session)

    async def _serve(self, session: Any) -> None:
        """Serve an already-created legacy session (also used by unit fixtures)."""
        init_result = await asyncio.wait_for(
            session.initialize(),
            timeout=self.connect_timeout,
        )
        await self._serve_connected(
            session,
            capabilities=getattr(init_result, "capabilities", None),
        )

    async def _serve_connected(self, session: Any, capabilities: Any = None) -> None:
        """Discover tools and supervise a negotiated session until it ends.

        Shared by all transports. A keepalive failure or a transport error
        reported by an in-flight tool call is propagated to the connection
        supervisor, which closes this transport context before reconnecting.
        """
        assert self.connection_failed_event is not None
        self.connection_failed_event.clear()
        self._connection_failure = None
        self.capabilities = capabilities or getattr(session, "server_capabilities", None)
        self.session = session
        await self._discover()
        if self._registry is not None:
            _reregister_server_tools(self)
        self._mark_connected(session)

        assert self.shutdown_event is not None
        if self._activity_event is None:
            self._activity_event = asyncio.Event()
        shutdown_wait = asyncio.create_task(self.shutdown_event.wait())
        connection_failed_wait = asyncio.create_task(self.connection_failed_event.wait())
        keepalive = asyncio.create_task(self._keepalive_loop())
        recycle = asyncio.create_task(self._recycle_when_due())
        try:
            done, _ = await asyncio.wait(
                {shutdown_wait, connection_failed_wait, keepalive, recycle},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_wait in done:
                return
            if connection_failed_wait in done:
                failure = self._connection_failure
                if failure is not None:
                    raise failure
                raise ConnectionError("MCP connection was reported unhealthy")
            if recycle in done:
                recycle.result()
                self._planned_recycle = True
                if self._connect_deadline is not None:
                    self._connect_deadline.reschedule(
                        asyncio.get_running_loop().time() + self._retry_policy.close_timeout,
                    )
                return
            # Keepalive is expected to live for the full connection lifetime.
            # Its clean exit is just as suspicious as an exception.
            failure = keepalive.exception()
            if failure is not None:
                raise failure
            raise ConnectionError("MCP keepalive stopped unexpectedly")
        finally:
            # Cancel AND await the background tasks so they unwind inside
            # this still-open transport context (avoids "Task was
            # destroyed but it is pending" warnings and ensures any
            # in-flight refresh RPC is torn down cleanly).
            pending = [
                shutdown_wait,
                connection_failed_wait,
                keepalive,
                recycle,
                *list(self._pending_refreshes),
            ]
            for task in pending:
                if not task.done():
                    task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if self.session is session:
                self.session = None

    async def _keepalive_loop(self) -> None:
        """Ping periodically; transport failures propagate to the supervisor."""
        while True:
            await asyncio.sleep(self._retry_policy.keepalive_interval)
            if self.session is None or self.rpc_lock is None:
                continue
            async with self.rpc_lock:
                await asyncio.wait_for(
                    self.session.list_tools(),
                    timeout=self._retry_policy.keepalive_timeout,
                )

    async def _discover(self) -> None:
        assert self.session is not None
        observed_at = time.time()
        credentials = None
        if self._manifest_store is not None:
            try:
                credentials = await asyncio.to_thread(self._manifest_store._credential_revision)
            except (OSError, ValueError):
                pass
        self._tool_pagination = await self._collect_tools()
        self.tools = list(self._tool_pagination.items)
        self._catalog_source = "live"
        if self._tool_pagination.truncated:
            logger.warning(
                "MCP server '%s': tool discovery truncated after %d pages/%d tools (%s)",
                self.name,
                self._tool_pagination.pages,
                len(self.tools),
                self._tool_pagination.reason,
            )
        elif self._manifest_store is not None and credentials is not None:
            try:
                await asyncio.to_thread(
                    self._manifest_store.save, self.tools, self.capabilities, observed_at,
                    expected_credentials=credentials,
                )
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                logger.debug("MCP manifest write skipped for '%s' (%s)", self.name, type(exc).__name__)

    async def _collect_tools(self) -> MCPPageCollection:
        """Collect the full bounded tool catalog from the active session."""
        assert self.session is not None

        async def _fetch(cursor: str | None) -> Any:
            if cursor is None:
                return await self.session.list_tools()
            return await self.session.list_tools(cursor=cursor)

        return await collect_mcp_pages(
            _fetch,
            "tools",
            max_pages=self.pagination_max_pages,
            max_items=self.pagination_max_items,
        )

    # ----- Dynamic tool discovery (tools/list_changed, D8) -------------

    def _make_message_handler(self):
        """Return a ``message_handler`` callback for ClientSession.

        Only ``ToolListChangedNotification`` triggers work; other
        notifications are ignored. The refresh runs in a detached task
        so the SDK's notification dispatch returns promptly and the
        stdio JSON-RPC stream doesn't wedge mid-notification.
        """

        async def _handler(message: Any) -> None:
            try:
                if isinstance(message, Exception):
                    from flowly.mcp.lifecycle import is_transport_failure

                    if is_transport_failure(message):
                        self.report_transport_failure(message, self.session)
                    return
                if not (_MCP_NOTIFICATIONS and isinstance(message, ServerNotification)):
                    return
                if isinstance(message.root, ToolListChangedNotification):
                    logger.info(
                        "MCP server '%s': tools/list_changed received",
                        self.name,
                    )
                    self._schedule_refresh()
                    await asyncio.sleep(0)
            except Exception as exc:
                logger.error(
                    "MCP server '%s' message handler error: %s", sanitize_error(self.name, limit=200),
                    exception_diagnostic(exc, secrets=diagnostic_secrets(self._config)),
                )

        return _handler

    def _schedule_refresh(self) -> None:
        task = asyncio.create_task(self._refresh_tools())
        self._pending_refreshes.add(task)
        task.add_done_callback(self._pending_refreshes.discard)

    async def _refresh_tools(self) -> None:
        """Re-fetch the tool list and re-register against the live registry."""
        if self._registry is None or self._refresh_lock is None:
            return
        try:
            async with self._refresh_lock:
                async with self.rpc_lock:  # type: ignore[arg-type]
                    await self._discover()
                if self._tool_pagination.truncated:
                    logger.warning(
                        "MCP server '%s': refreshed tool catalog truncated (%s)",
                        self.name,
                        self._tool_pagination.reason,
                    )
                # Re-registration touches the shared registry dict; the
                # discovery module owns that logic so we route through it.
                _reregister_server_tools(self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "MCP server '%s' dynamic refresh failed: %s", sanitize_error(self.name, limit=200),
                exception_diagnostic(exc, secrets=diagnostic_secrets(self._config)),
            )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _filter_remote_tool(
    server_cfg: dict[str, Any],
    remote_name: str,
) -> bool:
    tools_cfg = server_cfg.get("tools") or {}
    include = [str(x) for x in (tools_cfg.get("include") or [])]
    exclude = [str(x) for x in (tools_cfg.get("exclude") or [])]
    if include:
        return remote_name in include
    if exclude:
        return remote_name not in exclude
    return True


def _capability_advertised(server_task: MCPServerTask, attr: str) -> bool:
    """True if the server advertised the ``resources``/``prompts`` capability.

    Source of truth is ``initialize_result.capabilities`` — its sub-objects
    are non-None only when the server implements that request family.
    When capabilities weren't captured (older fixtures), default to True
    so we don't regress servers that were working before this gate.
    """
    caps = server_task.capabilities
    if caps is None:
        return True
    return getattr(caps, attr, None) is not None


def _utility_tools_for_server(
    server_task: MCPServerTask,
    server_cfg: dict[str, Any],
) -> list[Any]:
    """Build resource/prompt utility tools allowed by config + capabilities (D9)."""
    from flowly.mcp.tool import (
        MCPGetPromptTool,
        MCPListPromptsTool,
        MCPListResourcesTool,
        MCPReadResourceTool,
    )

    tools_cfg = server_cfg.get("tools") or {}
    want_resources = bool(tools_cfg.get("resources"))
    want_prompts = bool(tools_cfg.get("prompts"))

    out: list[Any] = []
    if want_resources and _capability_advertised(server_task, "resources"):
        out.append(MCPListResourcesTool(server_task=server_task))
        out.append(MCPReadResourceTool(server_task=server_task))
    if want_prompts and _capability_advertised(server_task, "prompts"):
        out.append(MCPListPromptsTool(server_task=server_task))
        out.append(MCPGetPromptTool(server_task=server_task))
    return out


def _register_tools_for_server(
    *,
    server_task: MCPServerTask,
    server_cfg: dict[str, Any],
    tool_registry: Any,
) -> list[str]:
    """Register MCP tools into Flowly's registry. Returns registered names."""
    with server_task._registry_lock:
        return _register_tools_for_server_locked(server_task, server_cfg, tool_registry)


def _register_tools_for_server_locked(
    server_task: MCPServerTask, server_cfg: dict[str, Any], tool_registry: Any,
) -> list[str]:
    from flowly.mcp.tool import MCPTool

    try:
        binding = server_task._registry_bindings.get(tool_registry)
    except TypeError:
        binding = None
    if binding is not None:
        # Repeated discovery against the same registry must not forget the
        # names we own merely because _try_register sees existing entries.
        server_task.bind_registry(tool_registry, server_cfg)
        registered = _sync_registry_tools(server_task, tool_registry, server_cfg, binding[1])
        server_task.set_registered_names(registered)
        return registered
    server_task.bind_registry(tool_registry, server_cfg)

    registered: list[str] = []

    def _try_register(tool: Any) -> None:
        if tool_registry.has(tool.name):
            logger.warning(
                "MCP server '%s': tool '%s' collides with an existing tool; "
                "keeping the existing entry.",
                server_task.name,
                tool.name,
            )
            return
        tool_registry.register(tool)
        registered.append(tool.name)

    for remote_tool in server_task.tools:
        remote_name = getattr(remote_tool, "name", "")
        if not remote_name or not _filter_remote_tool(server_cfg, remote_name):
            continue
        scan_description(
            server_task.name,
            remote_name,
            getattr(remote_tool, "description", "") or "",
            secrets=diagnostic_secrets(getattr(server_task, "_config", None)),
        )
        _try_register(MCPTool(server_task=server_task, remote_tool=remote_tool))

    for util_tool in _utility_tools_for_server(server_task, server_cfg):
        _try_register(util_tool)

    server_task.set_registered_names(registered)
    return registered


def _reregister_server_tools(server_task: MCPServerTask) -> None:
    """Re-sync a server's tools after a tools/list_changed notification (D8).

    Deregisters MCP tools that vanished, registers newly-appeared ones,
    and leaves unchanged tools in place (live tool-call IDs may point at
    existing handlers). Only touches tools this server owns.
    """
    with server_task._registry_lock:
        _reregister_bound_tools(server_task)


def _reregister_bound_tools(server_task: MCPServerTask) -> None:
    bindings = [(reg, cfg, names) for reg, (cfg, names)
                in list(server_task._registry_bindings.items())]
    if server_task._registry is not None and not any(
        reg is server_task._registry for reg, _, _ in bindings
    ):
        bindings.append((server_task._registry, server_task._server_cfg, server_task._registered_names))
    for registry, cfg, names in bindings:
        updated = _sync_registry_tools(server_task, registry, cfg, names)
        try:
            server_task._registry_bindings[registry] = (cfg, updated)
        except TypeError:
            pass
        if registry is server_task._registry:
            server_task._registered_names = updated


def _sync_registry_tools(
    server_task: MCPServerTask, registry: Any, server_cfg: dict[str, Any], names: list[str],
) -> list[str]:
    """Apply one authoritative catalog to one consumer's filters/ownership."""

    old_names = set(names)
    # Recompute what *should* be registered from the fresh tool list.
    from flowly.mcp.tool import MCPTool

    desired: dict[str, Any] = {}
    for remote_tool in server_task.tools:
        remote_name = getattr(remote_tool, "name", "")
        if not remote_name or not _filter_remote_tool(server_cfg, remote_name):
            continue
        tool = MCPTool(server_task=server_task, remote_tool=remote_tool)
        desired.setdefault(tool.name, tool)
    for util_tool in _utility_tools_for_server(server_task, server_cfg):
        desired[util_tool.name] = util_tool

    def _registered(name: str) -> Any | None:
        getter = getattr(registry, "get", None)
        if callable(getter):
            return getter(name)
        tools = getattr(registry, "tools", None)
        return tools.get(name) if isinstance(tools, dict) else None

    def _owned_by_server(tool: Any) -> bool:
        return getattr(tool, "_server_task", None) is server_task

    desired_names = set(desired)

    # Drop vanished tools only if the live registry entry is still ours. A
    # plugin may have replaced a formerly-owned name after registration; an
    # MCP refresh must never delete that newer foreign entry.
    for stale in old_names - desired_names:
        existing = _registered(stale)
        if existing is not None and _owned_by_server(existing):
            registry.unregister(stale)

    def _schema_fingerprint(tool: Any) -> str:
        try:
            if isinstance(tool, MCPTool):
                return tool.contract_fingerprint()
            schema = tool.to_schema()
            return json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            return ""

    # Register newcomers and replace same-name tools whose description or
    # input schema changed. Replacing the registry entry is safe for an
    # in-flight call: execute() already holds its old object reference, while
    # future calls and semantic indexing see the new immutable schema.
    new_names: list[str] = []
    changed: set[str] = set()
    for name in desired_names:
        if name in old_names:
            existing = _registered(name)
            if existing is None:
                registry.register(desired[name])
                changed.add(name)
            elif not _owned_by_server(existing):
                logger.warning(
                    "MCP server '%s': formerly-owned tool '%s' now belongs to "
                    "another source; skipping refresh.",
                    server_task.name,
                    name,
                )
                continue
            elif _schema_fingerprint(existing) != _schema_fingerprint(desired[name]):
                registry.register(desired[name])
                changed.add(name)
            new_names.append(name)
            continue
        if registry.has(name):
            logger.warning(
                "MCP server '%s': refreshed tool '%s' collides with an existing tool; skipping.",
                server_task.name,
                name,
            )
            continue
        registry.register(desired[name])
        new_names.append(name)

    added = desired_names - old_names
    removed = old_names - desired_names
    if added or removed or changed:
        logger.info(
            "MCP server '%s': tools changed — added %s, removed %s, updated %s",
            server_task.name,
            sorted(added) or "none",
            sorted(removed) or "none",
            sorted(changed) or "none",
        )
    return new_names


def _coerce_servers_input(
    servers: Any,
) -> dict[str, dict[str, Any]]:
    """Accept either a dict-of-Pydantic-models or a dict-of-dicts."""
    if not servers:
        return {}
    out: dict[str, dict[str, Any]] = {}
    items: Any
    if hasattr(servers, "items"):
        items = servers.items()
    else:
        items = servers
    for name, cfg in items:
        if hasattr(cfg, "model_dump"):
            out[name] = cfg.model_dump()
        elif isinstance(cfg, dict):
            out[name] = dict(cfg)
        else:
            logger.warning("MCP server '%s' has invalid config; skipping", name)
    return out


async def _shared_server(
    name: str, config: dict[str, Any], identity: str, registry: Any, interactive: bool,
) -> list[str]:
    """One initial startup per name/context; cancelled waiters don't own peers."""
    reuse = None
    with _servers_lock:
        if _shutting_down:
            raise MCPUnavailableError("MCP servers are shutting down")
        existing = _servers.get(name)
        startup = _starting.get(name)
        existing_identity = startup.identity if startup is not None else (
            existing._config_identity if existing is not None else None
        )
        if existing is not None and existing_identity != identity:
            raise MCPUnavailableError(
                f"MCP server '{name}' is already configured differently; "
                "shut down/reload servers before changing its profile, credentials or policy"
            )
        if existing is not None and startup is None:
            if existing._task is None or existing._task.done():
                raise MCPUnavailableError(f"MCP server '{name}' stopped; reload configured servers")
            reuse = existing
        if startup is None and reuse is None:
            server = MCPServerTask(name)
            server.interactive = interactive
            startup = _Startup(server, identity)
            _starting[name] = startup
            _servers[name] = server

            async def initialize() -> MCPServerTask:
                try:
                    await server.start(config, use_manifest=not interactive, expected_identity=identity)
                    return server
                except BaseException:
                    await server.shutdown()
                    with _servers_lock:
                        if _servers.get(name) is server:
                            _servers.pop(name, None)
                    raise
                finally:
                    with _servers_lock:
                        if _starting.get(name) is startup:
                            _starting.pop(name, None)

            startup.task = asyncio.create_task(initialize(), name=f"mcp-start-{name}")
            startup.task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        if startup is not None:
            startup.waiters += 1

    if reuse is not None:
        if interactive and reuse.session is None:
            # Explicit discovery must check a dormant runtime, not just return
            # its old hints. Do not upgrade another owner's OAuth interactivity;
            # the standalone login/probe owns interactive credential recovery.
            async with reuse.connection_lease():
                pass
        return _register_tools_for_server(
            server_task=reuse, server_cfg=config, tool_registry=registry,
        )
    assert startup is not None and startup.task is not None
    try:
        server = await asyncio.shield(startup.task)
        with _servers_lock:
            if _shutting_down or _servers.get(name) is not server:
                raise MCPUnavailableError("MCP startup was superseded by shutdown")
        return _register_tools_for_server(
            server_task=server, server_cfg=config, tool_registry=registry,
        )
    finally:
        startup.waiters -= 1
        if not startup.waiters and not startup.task.done():
            startup.task.cancel()
            await asyncio.gather(startup.task, return_exceptions=True)
            # A task cancelled before its first bytecode never enters its
            # initialize/finally block. Retire that reservation explicitly.
            await startup.server.shutdown()
            with _servers_lock:
                if _starting.get(name) is startup:
                    _starting.pop(name, None)
                if _servers.get(name) is startup.server:
                    _servers.pop(name, None)


def discover_mcp_tools(
    *, servers: Any, tool_registry: Any, interactive: bool = False,
) -> list[str]:
    """Register enabled servers, sharing in-flight startup across callers.

    A valid opt-in manifest can advertise cached schemas without launching a
    transport. Actual calls always acquire a newly verified live connection.
    Interactive discovery and the standalone probe never use cached readiness.
    """
    if not _MCP_AVAILABLE:
        return []
    raw = _coerce_servers_input(servers)
    if not raw:
        return []
    try:
        from flowly.mcp.env_loader import load_flowly_dotenv

        load_flowly_dotenv()
    except Exception as exc:
        logger.debug("MCP .env loader skipped (%s)", type(exc).__name__)

    from flowly.mcp.manifest import configuration_fingerprint
    from flowly.profile import get_flowly_home

    home = get_flowly_home().expanduser().resolve()
    enabled = {}
    for name, cfg in raw.items():
        if not cfg.get("enabled", True) or not sanitize_mcp_name_component(name):
            continue
        try:
            resolved = interpolate_env_vars(cfg)
            enabled[name] = (resolved, configuration_fingerprint(name, resolved, home))
        except (ValueError, TypeError) as exc:
            # Validation errors can contain raw input values: never log them.
            logger.warning("MCP server '%s': invalid configuration (%s)", name, type(exc).__name__)
    if not enabled:
        return []
    with _servers_lock:
        if _shutting_down:
            logger.warning("MCP discovery deferred: servers are shutting down")
            return []
    loop = _ensure_loop()

    async def connect_all():
        return await asyncio.gather(*[
            _shared_server(name, cfg, identity, tool_registry, interactive)
            for name, (cfg, identity) in enabled.items()
        ], return_exceptions=True)

    future = asyncio.run_coroutine_threadsafe(connect_all(), loop)
    try:
        results = future.result(timeout=180)
    except Exception as exc:
        future.cancel()  # Last startup waiter owns cancellation and joined cleanup.
        logger.warning("MCP discovery aborted: %s", sanitize_error(str(exc)))
        return []

    registered = []
    for name, result in zip(enabled, results):
        if isinstance(result, BaseException):
            logger.warning(
                "MCP server '%s' discovery failed: %s", sanitize_error(name, limit=200),
                exception_diagnostic(result, secrets=diagnostic_secrets(enabled[name][0])),
            )
        else:
            registered.extend(result)
    return registered


def shutdown_mcp_servers(timeout: float = 10.0) -> None:
    """Close registered and still-starting servers before retiring their loop.

    A caller's observation timeout does not abandon asynchronous cleanup or
    allow a replacement startup while that cleanup is still in progress.
    """
    global _shutting_down
    with _servers_lock:
        loop = _loop
        if loop is None or _shutting_down or not _servers:
            return
        _shutting_down = True
        servers = list(_servers.values())
        startups = [item.task for item in _starting.values() if item.task is not None]

    async def shutdown_all():
        try:
            for task in startups:
                task.cancel()
            await asyncio.gather(*startups, return_exceptions=True)
            await asyncio.gather(*(server.shutdown() for server in servers), return_exceptions=True)
        finally:
            with _servers_lock:
                _servers.clear()
                _starting.clear()

    future = asyncio.run_coroutine_threadsafe(shutdown_all(), loop)

    def retire_loop(_done):
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass

    future.add_done_callback(retire_loop)
    try:
        future.result(timeout=timeout)
    except TimeoutError:
        logger.warning("MCP shutdown is still draining in the background")
        return
    except Exception as exc:
        logger.debug("MCP shutdown failed (%s)", type(exc).__name__)
    # The completion callback already scheduled stop. A second stop can land
    # during shutdown_asyncgens and interrupt resource finalization.
    _stop_loop(expected_loop=loop, request_stop=False)
