"""Connect-once MCP probe — shared by the CLI (`mcp add --probe` / `mcp test`),
the OAuth login flow, and the feature-RPC ``mcp.test`` method.

The probe reuses the production client path (:class:`MCPServerTask`) so what it
reports matches exactly what the agent will see when it connects at boot. It
spins the server up, lists its tools, and tears it down — no registration, no
side effects on the running agent.
"""

from __future__ import annotations

import asyncio
import math
from concurrent.futures import Future

_PROBE_SHUTDOWN_GRACE_SECONDS = 30.0
_POST_AUTH_CONNECT_GRACE_SECONDS = 60.0


def _submit_probe(
    name: str,
    cfg_dump: dict,
    *,
    interactive: bool,
) -> tuple[Future[list[str]] | None, float, tuple[bool, list[str], str] | None]:
    """Submit one probe to the dedicated MCP loop.

    Returns ``(future, outer_timeout, immediate_result)``.  Keeping submission
    separate lets synchronous CLI callers and asynchronous feature-RPC callers
    share exactly the same connection path without either blocking the
    gateway's event loop or duplicating lifecycle/error handling.
    """
    try:
        from flowly.mcp.client import (
            _MCP_AVAILABLE,
            MCPServerTask,
            _ensure_loop,
        )
        from flowly.mcp.env_loader import load_flowly_dotenv
        from flowly.mcp.security import interpolate_env_vars
    except ImportError as exc:
        return (
            None,
            0.0,
            (
                False,
                [],
                f"MCP runtime not importable: {exc}",
            ),
        )

    if not _MCP_AVAILABLE:
        return (
            None,
            0.0,
            (
                False,
                [],
                "mcp SDK is not installed (`pip install mcp`)",
            ),
        )

    load_flowly_dotenv()
    cfg = interpolate_env_vars(dict(cfg_dump))

    try:
        connect_timeout = float(cfg.get("connect_timeout", 60.0))
    except (TypeError, ValueError):
        connect_timeout = float("nan")
    if not math.isfinite(connect_timeout) or connect_timeout <= 0:
        return (
            None,
            0.0,
            (
                False,
                [],
                "invalid MCP connect_timeout: expected a positive finite number",
            ),
        )

    # An interactive OAuth flow blocks on a human authorizing in the
    # browser. The callback itself owns a five-minute human-interaction
    # deadline; connection initialization needs a separate grace period after
    # the callback so the two deadlines do not race at exactly 300 seconds.
    if interactive and cfg.get("auth") == "oauth":
        from flowly.mcp.oauth import OAUTH_CALLBACK_TIMEOUT_SECONDS

        # Interactive login is a bounded operation independent of the
        # server's boot-time connect deadline. A very large persisted timeout
        # must not make a desktop sign-in RPC wait indefinitely.
        connect_timeout = OAUTH_CALLBACK_TIMEOUT_SECONDS + _POST_AUTH_CONNECT_GRACE_SECONDS
        cfg["connect_timeout"] = connect_timeout

    outer_timeout = connect_timeout + _PROBE_SHUTDOWN_GRACE_SECONDS
    loop = _ensure_loop()

    async def _connect_and_list() -> list[str]:
        task = MCPServerTask(name)
        task.interactive = interactive
        try:
            await task.start(cfg)
            return [getattr(tool, "name", "?") for tool in task.tools]
        finally:
            await task.shutdown()

    future = asyncio.run_coroutine_threadsafe(_connect_and_list(), loop)
    return future, outer_timeout, None


def _probe_failure(name: str, cfg_dump: dict, exc: BaseException) -> tuple[bool, list[str], str]:
    """Render a sanitized, actionable probe error."""
    from flowly.mcp.security import sanitize_error

    detail = sanitize_error(_exception_detail(exc))
    if cfg_dump.get("command"):
        detail += " (server output: $FLOWLY_HOME/logs/mcp-stderr.log)"
    return False, [], f"connect failed: {detail}"


def _exception_detail(exc: BaseException) -> str:
    """Flatten exception groups so transport root causes are not hidden.

    anyio-based MCP transports commonly wrap subprocess/HTTP errors in an
    ``ExceptionGroup`` whose plain ``str()`` only says "1 sub-exception".
    Keep the group context while including bounded, de-duplicated leaf errors.
    """
    parts: list[str] = []

    def _walk(current: BaseException) -> None:
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            for child in nested:
                if isinstance(child, BaseException):
                    _walk(child)
            return
        message = str(current).strip()
        rendered = message or type(current).__name__
        if rendered not in parts:
            parts.append(rendered)

    _walk(exc)
    if not parts:
        return repr(exc)
    return "; ".join(parts[:6])


def _probe_timeout(name: str, timeout: float) -> tuple[bool, list[str], str]:
    return (
        False,
        [],
        f"connect failed: MCP probe for server '{name}' timed out after {timeout:.0f}s",
    )


def probe_tool_names(
    name: str,
    cfg_dump: dict,
    *,
    interactive: bool = False,
) -> tuple[bool, list[str], str]:
    """Connect once to a server config and return ``(ok, tool_names, error)``.

    ``cfg_dump`` is a snake_case server config dict (e.g. ``MCPServerConfig`` via
    ``model_dump()``) — env ``${VAR}`` placeholders are interpolated here.
    ``interactive`` lets an OAuth-configured server launch the browser flow.
    On failure returns ``(False, [], message)`` with credentials redacted.
    """
    future, outer_timeout, immediate = _submit_probe(
        name,
        cfg_dump,
        interactive=interactive,
    )
    if immediate is not None:
        return immediate
    assert future is not None
    try:
        tool_names = future.result(timeout=outer_timeout)
    except TimeoutError as exc:
        # An inner MCP connect timeout arrives on an already-completed future
        # and carries its own precise error.  Only an unfinished future means
        # this outer watchdog actually fired.
        if future.done():
            return _probe_failure(name, cfg_dump, exc)
        future.cancel()
        return _probe_timeout(name, outer_timeout)
    except Exception as exc:
        return _probe_failure(name, cfg_dump, exc)
    return True, tool_names, ""


async def probe_tool_names_async(
    name: str,
    cfg_dump: dict,
    *,
    interactive: bool = False,
) -> tuple[bool, list[str], str]:
    """Async probe variant for gateway/relay feature RPCs.

    MCP work still runs on the dedicated MCP loop.  This method only awaits its
    thread-safe future without blocking the caller's event loop, preserving
    WebSocket heartbeats and unrelated RPC traffic during browser auth.
    Cancellation propagates to the MCP loop so abandoned desktop requests do
    not leave an orphaned auth flow behind.
    """
    future, outer_timeout, immediate = _submit_probe(
        name,
        cfg_dump,
        interactive=interactive,
    )
    if immediate is not None:
        return immediate
    assert future is not None

    wrapped = asyncio.wrap_future(future)
    try:
        done, _ = await asyncio.wait({wrapped}, timeout=outer_timeout)
        if not done:
            future.cancel()
            return _probe_timeout(name, outer_timeout)
        try:
            tool_names = wrapped.result()
        except Exception as exc:
            return _probe_failure(name, cfg_dump, exc)
        return True, tool_names, ""
    except asyncio.CancelledError:
        future.cancel()
        raise


def probe_message(name: str, cfg_dump: dict, *, interactive: bool = False) -> tuple[bool, str]:
    """Connect once and return ``(ok, human_message)`` with a tool preview."""
    ok, tool_names, error = probe_tool_names(name, cfg_dump, interactive=interactive)
    return _probe_message_result(name, ok, tool_names, error)


async def probe_message_async(
    name: str,
    cfg_dump: dict,
    *,
    interactive: bool = False,
) -> tuple[bool, str]:
    """Async message-shaped probe for feature RPC callers."""
    ok, tool_names, error = await probe_tool_names_async(
        name,
        cfg_dump,
        interactive=interactive,
    )
    return _probe_message_result(name, ok, tool_names, error)


def _probe_message_result(
    name: str,
    ok: bool,
    tool_names: list[str],
    error: str,
) -> tuple[bool, str]:
    if not ok:
        return False, error
    if not tool_names:
        return True, f"connected; server '{name}' reported no tools"
    preview = ", ".join(tool_names[:8])
    if len(tool_names) > 8:
        preview += f", … (+{len(tool_names) - 8} more)"
    return True, f"connected; {len(tool_names)} tool(s): {preview}"
