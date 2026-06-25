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
import logging
import threading
from typing import Any
from urllib.parse import urlparse

from flowly.mcp.schema import (
    sanitize_mcp_name_component,
)
from flowly.mcp.security import (
    build_safe_env,
    interpolate_env_vars,
    sanitize_error,
    scan_description,
)
from flowly.mcp.stderr_log import (
    get_stderr_log,
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
# Fallback if mcp.types.LATEST_PROTOCOL_VERSION isn't exported by this SDK
# build. Streamable HTTP was introduced in 2025-03-26 so this is a safe
# floor for the HTTP transport path even on older SDKs.
LATEST_PROTOCOL_VERSION = "2025-03-26"

try:
    from mcp import ClientSession, StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client  # type: ignore
    _MCP_AVAILABLE = True
    try:
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore
        _MCP_HTTP_AVAILABLE = True
    except ImportError:
        _MCP_HTTP_AVAILABLE = False
    # SSE transport (T3) — older HTTP-style servers. Optional.
    try:
        from mcp.client.sse import sse_client  # type: ignore
        _MCP_SSE_AVAILABLE = True
    except ImportError:
        sse_client = None  # type: ignore
        _MCP_SSE_AVAILABLE = False
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION  # type: ignore # noqa: F811
    except ImportError:
        pass
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
        _MCP_MESSAGE_HANDLER = (
            "message_handler" in _inspect.signature(ClientSession).parameters
        )
    except (TypeError, ValueError):
        _MCP_MESSAGE_HANDLER = False
except ImportError:
    logger.debug("mcp SDK not installed — MCP discovery disabled")


# ---------------------------------------------------------------------------
# Background event loop singleton
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()
_servers: dict[str, "MCPServerTask"] = {}


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

# Keepalive interval (T8). Must be shorter than typical LB / NAT idle
# timeouts (commonly 300-600s) so long-idle HTTP connections stay warm.
_KEEPALIVE_INTERVAL_SEC = 180.0


def _bump_server_error(server_name: str) -> None:
    import time
    with _breaker_lock:
        count = _server_error_counts.get(server_name, 0) + 1
        _server_error_counts[server_name] = count
        if count >= _CIRCUIT_BREAKER_THRESHOLD:
            _server_breaker_opened_at[server_name] = time.monotonic()


def _reset_server_error(server_name: str) -> None:
    with _breaker_lock:
        _server_error_counts.pop(server_name, None)
        _server_breaker_opened_at.pop(server_name, None)


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
            return None  # half-open: allow a probe
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


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start the MCP background event loop if not already running."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop

        ready = threading.Event()
        loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            loop_holder["loop"] = loop
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_runner, name="flowly-mcp-loop", daemon=True)
        thread.start()
        ready.wait()
        _loop = loop_holder["loop"]
        _loop_thread = thread
        return _loop


def _stop_loop() -> None:
    """Stop the MCP background loop (best effort). Used by shutdown."""
    global _loop, _loop_thread
    with _loop_lock:
        loop = _loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
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
            f"MCP server '{server_name}': url must be a string, got "
            f"{type(url).__name__}"
        )
    stripped = url.strip()
    if not stripped:
        raise InvalidMCPUrlError(f"MCP server '{server_name}': empty url")
    try:
        parsed = urlparse(stripped)
    except Exception as exc:
        raise InvalidMCPUrlError(
            f"MCP server '{server_name}': invalid url ({exc})"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidMCPUrlError(
            f"MCP server '{server_name}': scheme must be http or https, "
            f"got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise InvalidMCPUrlError(
            f"MCP server '{server_name}': missing host in {stripped!r}"
        )
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
        self.capabilities: Any | None = None
        # When True, an HTTP server configured for OAuth may launch the
        # interactive browser flow. False (agent boot) restricts OAuth to
        # silently using stored/refreshable tokens.
        self.interactive: bool = False
        self._config: dict[str, Any] = {}
        self._task: asyncio.Task[Any] | None = None
        # Asyncio primitives MUST be created on the loop they belong to.
        # We allocate them lazily in ``_run`` when the loop is known.
        self.ready: asyncio.Event | None = None
        self.shutdown_event: asyncio.Event | None = None
        self.rpc_lock: asyncio.Lock | None = None
        self.error: BaseException | None = None
        # Set by the discovery layer so dynamic tools/list_changed
        # refreshes (D8) can re-register into the live registry.
        self._registry: Any | None = None
        self._server_cfg: dict[str, Any] = {}
        self._registered_names: list[str] = []
        self._refresh_lock: asyncio.Lock | None = None
        self._pending_refreshes: set[asyncio.Task[Any]] = set()

    def is_http(self) -> bool:
        return bool(self._config.get("url"))

    def bind_registry(self, registry: Any, server_cfg: dict[str, Any]) -> None:
        """Record the registry + config used for dynamic re-registration."""
        self._registry = registry
        self._server_cfg = server_cfg

    def set_registered_names(self, names: list[str]) -> None:
        self._registered_names = list(names)

    async def start(self, config: dict[str, Any]) -> None:
        """Spawn the run-task on the current loop and wait for readiness."""
        self._config = config
        self.tool_timeout = float(config.get("timeout", 120.0))
        self.connect_timeout = float(config.get("connect_timeout", 60.0))
        self.ready = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self.rpc_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

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
        finally:
            if not ready_wait.done():
                ready_wait.cancel()

        if self.ready.is_set():
            return

        # Either timed out or the run-task already exited with an error.
        if self._task.done():
            exc = self._task.exception()
            if exc is not None:
                raise exc
        # Timeout: cancel the in-flight task so the transport closes.
        self._task.cancel()
        raise asyncio.TimeoutError(
            f"MCP server '{self.name}' connect timed out after "
            f"{self.connect_timeout:.0f}s"
        )

    async def shutdown(self) -> None:
        """Ask the run-task to exit, then wait for it."""
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _run(self) -> None:
        try:
            if self.is_http():
                await self._run_http()
            else:
                await self._run_stdio()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            logger.warning(
                "MCP server '%s' run failed: %s", self.name, sanitize_error(str(exc)),
            )

    def _session_kwargs(self) -> dict[str, Any]:
        """Build ClientSession kwargs — list_changed handler + sampling."""
        kwargs: dict[str, Any] = {}
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
                logger.debug("MCP sampling callback unavailable: %s", exc)
        return kwargs

    async def _run_stdio(self) -> None:
        if not _MCP_AVAILABLE:
            raise ImportError("mcp SDK is not installed")

        command = self._config.get("command") or ""
        if not command:
            raise ValueError(
                f"MCP server '{self.name}': stdio entry needs 'command'"
            )

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

        write_stderr_log_header(self.name)
        errlog = get_stderr_log()

        # Orphan reap (S7) is OPT-IN per server (reap_orphans). Default
        # off: the spawn-window child diff can, in rare races, attribute
        # an unrelated subprocess (e.g. a concurrent exec-tool bash) to
        # this server, and force-killing the wrong PID is destructive.
        # The MCP SDK already tears the child down on normal exit; this
        # only helps the Linux setsid-escapes-on-cancel edge case.
        reap = bool(self._config.get("reap_orphans"))
        if not reap:
            async with stdio_client(server_params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, **self._session_kwargs()) as session:
                    await self._serve(session)
            return

        from flowly.mcp.proc import snapshot_child_pids, reap_pids
        before = snapshot_child_pids()
        spawned: set[int] = set()
        try:
            async with stdio_client(server_params, errlog=errlog) as (read, write):
                spawned = snapshot_child_pids() - before
                async with ClientSession(read, write, **self._session_kwargs()) as session:
                    await self._serve(session)
        finally:
            # Runs on clean exit, error, and cancellation. If the SDK's
            # own teardown already reaped the child, reap_pids is a no-op.
            reap_pids(spawned, self.name)

    def _use_sse(self) -> bool:
        """Decide whether to use the SSE transport for this HTTP server."""
        return str(self._config.get("transport") or "auto").lower() == "sse"

    async def _run_http(self) -> None:
        url = _validate_http_url(self.name, self._config.get("url"))
        headers = dict(self._config.get("headers") or {})
        # Some servers require the MCP-Protocol-Version header on the
        # initial POST. Inject as a default; preserve user casing.
        if not any(k.lower() == "mcp-protocol-version" for k in headers):
            headers["mcp-protocol-version"] = LATEST_PROTOCOL_VERSION

        # OAuth 2.1 / PKCE (Faz 2b): when configured, attach the SDK's
        # OAuthClientProvider as the httpx auth flow. It transparently
        # uses stored tokens, refreshes them, and (interactive only)
        # runs the browser authorization flow on first use.
        auth = None
        if str(self._config.get("auth") or "") == "oauth":
            from flowly.mcp.oauth import build_oauth_provider
            auth = build_oauth_provider(
                self.name, url,
                interactive=self.interactive,
                scope=self._config.get("scope") or None,
            )
            if auth is None:
                raise ImportError(
                    "OAuth configured but this 'mcp' SDK build lacks "
                    "mcp.client.auth — upgrade the package."
                )

        # mTLS / custom CA (Faz 2c): only build a custom httpx factory
        # when the config actually sets a TLS knob, so the default path
        # keeps using the SDK's own factory.
        from flowly.mcp.tls import needs_custom_tls, make_http_client_factory
        client_factory = None
        if needs_custom_tls(self._config):
            client_factory = make_http_client_factory(self.name, self._config)

        if self._use_sse():
            await self._run_sse(url, headers, auth, client_factory)
            return

        if not _MCP_HTTP_AVAILABLE:
            raise ImportError(
                "HTTP MCP transport unavailable — upgrade the 'mcp' package "
                "to a version that exports mcp.client.streamable_http."
            )
        kwargs: dict[str, Any] = {"headers": headers, "auth": auth}
        if client_factory is not None:
            kwargs["httpx_client_factory"] = client_factory
        async with streamablehttp_client(url, **kwargs) as (read, write, _):
            async with ClientSession(read, write, **self._session_kwargs()) as session:
                await self._serve(session)

    async def _run_sse(self, url, headers, auth, client_factory) -> None:
        if not _MCP_SSE_AVAILABLE or sse_client is None:
            raise ImportError(
                "SSE MCP transport unavailable — upgrade the 'mcp' package "
                "to a version that exports mcp.client.sse."
            )
        kwargs: dict[str, Any] = {"headers": headers, "auth": auth}
        if client_factory is not None:
            kwargs["httpx_client_factory"] = client_factory
        async with sse_client(url, **kwargs) as (read, write):
            async with ClientSession(read, write, **self._session_kwargs()) as session:
                await self._serve(session)

    async def _serve(self, session: Any) -> None:
        """Initialize, discover, signal ready, then serve until shutdown.

        Shared by both transports. Runs a keepalive loop concurrently
        with the shutdown wait so long-idle connections stay warm (T8).
        """
        init_result = await asyncio.wait_for(
            session.initialize(), timeout=self.connect_timeout,
        )
        self.capabilities = getattr(init_result, "capabilities", None)
        self.session = session
        await self._discover()
        assert self.ready is not None
        self.ready.set()

        assert self.shutdown_event is not None
        shutdown_wait = asyncio.create_task(self.shutdown_event.wait())
        keepalive = asyncio.create_task(self._keepalive_loop())
        try:
            await shutdown_wait
        finally:
            # Cancel AND await the background tasks so they unwind inside
            # this still-open transport context (avoids "Task was
            # destroyed but it is pending" warnings and ensures any
            # in-flight refresh RPC is torn down cleanly).
            pending = [keepalive, *list(self._pending_refreshes)]
            for task in pending:
                if not task.done():
                    task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _keepalive_loop(self) -> None:
        """Ping ``list_tools`` periodically to keep the transport warm.

        On failure we log and stop pinging — the connection is already
        dead, and the next real tool call surfaces the error through the
        circuit breaker. Full reconnect is a later phase.
        """
        while True:
            try:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise
            if self.session is None or self.rpc_lock is None:
                continue
            try:
                async with self.rpc_lock:
                    await asyncio.wait_for(self.session.list_tools(), timeout=30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "MCP server '%s' keepalive ping failed: %s",
                    self.name, sanitize_error(str(exc) or repr(exc)),
                )
                return

    async def _discover(self) -> None:
        assert self.session is not None
        result = await self.session.list_tools()
        self.tools = list(getattr(result, "tools", []) or [])

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
                    return
                if not (_MCP_NOTIFICATIONS and isinstance(message, ServerNotification)):
                    return
                if isinstance(message.root, ToolListChangedNotification):
                    logger.info(
                        "MCP server '%s': tools/list_changed received", self.name,
                    )
                    self._schedule_refresh()
                    await asyncio.sleep(0)
            except Exception:
                logger.exception("MCP server '%s' message handler error", self.name)

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
                    result = await self.session.list_tools()
                self.tools = list(getattr(result, "tools", []) or [])
                # Re-registration touches the shared registry dict; the
                # discovery module owns that logic so we route through it.
                _reregister_server_tools(self)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP server '%s' dynamic refresh failed", self.name)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _filter_remote_tool(
    server_cfg: dict[str, Any], remote_name: str,
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
    server_task: MCPServerTask, server_cfg: dict[str, Any],
) -> list[Any]:
    """Build resource/prompt utility tools allowed by config + capabilities (D9)."""
    from flowly.mcp.tool import (
        MCPListPromptsTool,
        MCPListResourcesTool,
        MCPReadResourceTool,
        MCPGetPromptTool,
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
    from flowly.mcp.tool import MCPTool

    server_task.bind_registry(tool_registry, server_cfg)

    registered: list[str] = []

    def _try_register(tool: Any) -> None:
        if tool_registry.has(tool.name):
            logger.warning(
                "MCP server '%s': tool '%s' collides with an existing tool; "
                "keeping the existing entry.",
                server_task.name, tool.name,
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
    registry = server_task._registry
    if registry is None:
        return

    old_names = set(server_task._registered_names)
    # Recompute what *should* be registered from the fresh tool list.
    from flowly.mcp.tool import MCPTool

    server_cfg = server_task._server_cfg
    desired: dict[str, Any] = {}
    for remote_tool in server_task.tools:
        remote_name = getattr(remote_tool, "name", "")
        if not remote_name or not _filter_remote_tool(server_cfg, remote_name):
            continue
        tool = MCPTool(server_task=server_task, remote_tool=remote_tool)
        desired[tool.name] = tool
    for util_tool in _utility_tools_for_server(server_task, server_cfg):
        desired[util_tool.name] = util_tool

    desired_names = set(desired)

    # Drop tools that disappeared (only ones THIS server registered).
    for stale in old_names - desired_names:
        registry.unregister(stale)

    # Register newcomers, respecting collisions with non-MCP tools.
    new_names: list[str] = []
    for name in desired_names:
        if name in old_names:
            new_names.append(name)
            continue
        if registry.has(name):
            logger.warning(
                "MCP server '%s': refreshed tool '%s' collides with an "
                "existing tool; skipping.", server_task.name, name,
            )
            continue
        registry.register(desired[name])
        new_names.append(name)

    server_task.set_registered_names(new_names)

    added = desired_names - old_names
    removed = old_names - desired_names
    if added or removed:
        logger.info(
            "MCP server '%s': tools changed — added %s, removed %s",
            server_task.name, sorted(added) or "none", sorted(removed) or "none",
        )


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


def discover_mcp_tools(
    *,
    servers: Any,
    tool_registry: Any,
    interactive: bool = False,
) -> list[str]:
    """Connect to all enabled MCP servers and register their tools.

    Called once at agent boot. Per-server failures are isolated: a
    broken stdio command for server A does not prevent server B from
    registering. Returns the list of registered (prefixed) tool names.

    ``interactive`` is False at agent boot so OAuth servers only use
    stored/refreshable tokens; the CLI passes True so the browser flow
    can run.

    No-op if the ``mcp`` SDK is not importable or no servers configured.
    """
    if not _MCP_AVAILABLE:
        logger.debug("MCP SDK unavailable — skipping discovery")
        return []

    raw = _coerce_servers_input(servers)
    if not raw:
        return []

    # Load $FLOWLY_HOME/.env so ${VAR} placeholders in config resolve.
    try:
        from flowly.mcp.env_loader import load_flowly_dotenv
        load_flowly_dotenv()
    except Exception as exc:
        logger.debug("MCP .env loader skipped: %s", exc)

    enabled: dict[str, dict[str, Any]] = {}
    already_connected: dict[str, dict[str, Any]] = {}
    for name, cfg in raw.items():
        sanitized_name = sanitize_mcp_name_component(name)
        if not cfg.get("enabled", True):
            logger.info("MCP server '%s' disabled — skipping", name)
            continue
        if not sanitized_name:
            logger.warning("MCP server name %r sanitizes to empty; skipping", name)
            continue
        resolved = interpolate_env_vars(cfg)
        if name in _servers:
            # A live server already exists in this process (e.g. a second
            # AgentLoop with a fresh registry). Don't reconnect — just
            # re-register its existing tools into the new registry so it
            # isn't left without them.
            already_connected[name] = resolved
            continue
        enabled[name] = resolved

    registered: list[str] = []

    # Re-register tools of already-connected servers into THIS registry.
    for name, cfg in already_connected.items():
        server = _servers.get(name)
        if server is None:
            continue
        registered.extend(_register_tools_for_server(
            server_task=server, server_cfg=cfg, tool_registry=tool_registry,
        ))

    if not enabled:
        if registered:
            logger.info(
                "MCP: re-registered %d tool(s) from %d already-connected "
                "server(s)", len(registered), len(already_connected),
            )
        return registered

    loop = _ensure_loop()

    async def _connect_all() -> dict[str, MCPServerTask | BaseException]:
        async def _connect_one(name: str, cfg: dict[str, Any]) -> MCPServerTask:
            task = MCPServerTask(name)
            task.interactive = interactive
            await task.start(cfg)
            return task

        results: dict[str, MCPServerTask | BaseException] = {}
        coros = {
            name: _connect_one(name, cfg) for name, cfg in enabled.items()
        }
        gathered = await asyncio.gather(*coros.values(), return_exceptions=True)
        for name, result in zip(coros.keys(), gathered):
            results[name] = result
        return results

    future = asyncio.run_coroutine_threadsafe(_connect_all(), loop)
    try:
        results = future.result(timeout=180)
    except Exception as exc:
        logger.warning("MCP discovery aborted: %s", sanitize_error(str(exc)))
        return registered

    for name, result in results.items():
        if isinstance(result, BaseException):
            logger.warning(
                "MCP server '%s' connect failed: %s",
                name, sanitize_error(str(result) or repr(result)),
            )
            continue
        _servers[name] = result
        registered.extend(_register_tools_for_server(
            server_task=result,
            server_cfg=enabled[name],
            tool_registry=tool_registry,
        ))

    if registered:
        logger.info(
            "MCP: registered %d tool(s) from %d server(s): %s",
            len(registered),
            sum(1 for r in results.values() if not isinstance(r, BaseException)),
            ", ".join(registered),
        )
    return registered


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def shutdown_mcp_servers(timeout: float = 10.0) -> None:
    """Tear down all registered MCP servers. Best-effort."""
    if _loop is None or not _servers:
        return

    async def _shutdown_all() -> None:
        await asyncio.gather(
            *(srv.shutdown() for srv in _servers.values()),
            return_exceptions=True,
        )

    try:
        future = asyncio.run_coroutine_threadsafe(_shutdown_all(), _loop)
        future.result(timeout=timeout)
    except Exception as exc:
        logger.debug("MCP shutdown errors (ignored): %s", exc)

    _servers.clear()
    _stop_loop()
