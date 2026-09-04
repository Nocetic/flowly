"""MCP server wiring for ``flowly mcp serve``.

Registers the read-plane tools (always) and, when ``allow_writes`` is set,
the gateway-backed write tools (Faz 3c). Runs on stdio.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_MCP_SERVER_AVAILABLE = False
try:
    from mcp import types as mcp_types  # type: ignore
    from mcp.server.auth.provider import AccessToken  # type: ignore
    from mcp.server.auth.settings import AuthSettings  # type: ignore
    from mcp.server.mcpserver import MCPServer  # type: ignore
    _MCP_SERVER_AVAILABLE = True
except ImportError:
    pass


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


class _StaticTokenVerifier:
    """Constant-time verifier for explicitly configured HTTP bearer tokens."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> Any:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="flowly-mcp-client",
            scopes=["flowly:mcp"],
        )


def _result(payload: dict[str, Any]) -> Any:
    """Return both readable text and native MCP structured content."""
    is_error = isinstance(payload.get("error"), str)
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(text=_dumps(payload))],
        structuredContent=payload,
        isError=is_error,
    )


def _is_loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def _validate_http_settings(
    *,
    host: str,
    port: int,
    path: str,
    auth_token: str,
    tls_cert: str = "",
    tls_key: str = "",
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("MCP HTTP port must be between 1 and 65535")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("MCP HTTP path must start with one '/' character")
    if auth_token and len(auth_token) < 32:
        raise ValueError("MCP HTTP bearer token must contain at least 32 characters")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("MCP HTTP TLS requires both a certificate and private key")
    for label, value in (("certificate", tls_cert), ("private key", tls_key)):
        if value and not Path(value).is_file():
            raise ValueError(f"MCP HTTP TLS {label} file does not exist: {value}")
    if not _is_loopback_host(host):
        if not auth_token:
            raise ValueError(
                "MCP HTTP on a non-loopback host requires a bearer token"
            )
        if not tls_cert:
            raise ValueError(
                "MCP HTTP on a non-loopback host requires TLS"
            )


def create_server(
    *,
    allow_writes: bool = False,
    auth_token: str = "",
    resource_url: str = "http://127.0.0.1:8765/mcp",
) -> Any:
    """Build the Flowly MCP server with read (and optional write) tools."""
    if not _MCP_SERVER_AVAILABLE:
        raise ImportError(
            "MCP server mode requires the 'mcp' package. "
            f"Install with: {sys.executable} -m pip install 'mcp'"
        )
    if auth_token and len(auth_token) < 32:
        raise ValueError("MCP HTTP bearer token must contain at least 32 characters")

    from flowly.mcp.server.readplane import (
        channels_list as _channels_list,
    )
    from flowly.mcp.server.readplane import (
        get_session_reader,
    )

    server_kwargs: dict[str, Any] = {}
    if auth_token:
        server_kwargs.update({
            "auth": AuthSettings(
                issuer_url=resource_url,
                resource_server_url=resource_url,
                required_scopes=["flowly:mcp"],
            ),
            "token_verifier": _StaticTokenVerifier(auth_token),
        })

    try:
        from flowly import __version__
    except Exception:
        __version__ = "0.0.0-dev"

    mcp = MCPServer(
        "flowly",
        title="Flowly Conversation Bridge",
        description="Access Flowly conversations and connected channels through MCP.",
        version=__version__,
        instructions=(
            "Flowly conversation bridge. Read conversation history across "
            "connected channels (Telegram, Discord, Slack, WhatsApp, web, "
            "email, Teams). Session keys are 'channel:chat_id'. "
            + (
                "Write operations are enabled and may affect external systems."
                if allow_writes
                else "This server is read-only."
            )
        ),
        **server_kwargs,
    )
    reader = get_session_reader()
    # Construct on first use, so listing tools alone does not create state.
    from functools import lru_cache

    @lru_cache(maxsize=1)
    def event_journal():
        from flowly.mcp.server.events import EventJournal
        return EventJournal(reader)

    read_annotations = mcp_types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    # -- read plane -----------------------------------------------------

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def conversations_list(
        platform: str | None = None,
        limit: int = 50,
        search: str | None = None,
        cursor: str | None = None,
    ) -> Any:
        """List Flowly conversations (most recent first).

        Args:
            platform: filter by channel (telegram, discord, slack, ...)
            limit: max conversations (default 50)
            search: filter by text in the session key or preview
            cursor: opaque cursor from a previous page
        """
        return _result(reader.conversations_list(platform, limit, search, cursor))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def conversation_get(session_key: str) -> Any:
        """Get metadata for one conversation by its 'channel:chat_id' key."""
        return _result(reader.conversation_get(session_key))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def messages_read(
        session_key: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Any:
        """Read recent user/assistant messages from a conversation.

        Args:
            session_key: the 'channel:chat_id' key from conversations_list
            limit: max messages, most recent (default 50)
            cursor: opaque cursor for the next older page
        """
        return _result(reader.messages_read(session_key, limit, cursor))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def messages_search(
        query: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Any:
        """Full-text search across all conversations (FTS5).

        Args:
            query: search text
            limit: max hits (default 20)
            cursor: opaque cursor from a previous page
        """
        return _result(reader.messages_search(query, limit, cursor))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def channels_list(platform: str | None = None) -> Any:
        """List configured channels and whether each is enabled."""
        return _result(_channels_list(platform))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def attachments_fetch(session_key: str, message_id: str) -> Any:
        """Describe a message's attachments using its message_id from messages_read.

        Returns metadata only; local paths and raw file bytes are not exposed.
        """
        return _result(reader.attachments_fetch(session_key, message_id))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    def events_poll(
        after_cursor: str | None = None, session_key: str | None = None, limit: int = 20,
    ) -> Any:
        """Poll conversation events after an opaque cursor. Omit cursor to start now.

        Keep next_cursor for the next poll, including across reconnects. Use the
        same session filter. CURSOR_EXPIRED means retained events were lost:
        refresh history before resuming with the returned cursor.
        """
        return _result(event_journal().poll(after_cursor, session_key, limit))

    @mcp.tool(annotations=read_annotations, structured_output=False)
    async def events_wait(
        after_cursor: str | None = None, session_key: str | None = None, timeout_ms: int = 30_000,
    ) -> Any:
        """Wait for conversation events, at most five minutes. Cancellation stops waiting.

        Shares cursor semantics with events_poll. Omit cursor to wait for new
        events from now. A timeout returns the cursor to resume from.
        """
        return _result(await event_journal().wait(after_cursor, session_key, timeout_ms))

    # -- write plane (Faz 3c) -------------------------------------------
    if allow_writes:
        try:
            from flowly.mcp.server.writeplane import register_write_tools
            register_write_tools(mcp, _result)
        except ImportError:
            logger.warning(
                "MCP serve: write tools requested but write plane is "
                "unavailable in this build; serving read-only.",
            )

    return mcp


def run_server(
    *,
    allow_writes: bool = False,
    verbose: bool = False,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    stateless: bool = False,
    auth_token: str = "",
    tls_cert: str = "",
    tls_key: str = "",
) -> None:
    """Create and run the Flowly MCP server on stdio or Streamable HTTP."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING, stream=sys.stderr,
    )
    normalized_transport = transport.strip().lower()
    if normalized_transport not in {"stdio", "http"}:
        raise ValueError("MCP server transport must be 'stdio' or 'http'")
    if normalized_transport == "stdio":
        server = create_server(allow_writes=allow_writes)
        server.run("stdio")
        return

    _validate_http_settings(
        host=host,
        port=port,
        path=path,
        auth_token=auth_token,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
    scheme = "https" if tls_cert else "http"
    resource_url = f"{scheme}://{host}:{port}{path}"
    server = create_server(
        allow_writes=allow_writes,
        auth_token=auth_token,
        resource_url=resource_url,
    )
    transport_kwargs = {
        "streamable_http_path": path,
        "json_response": True,
        "stateless_http": stateless,
    }
    if not tls_cert:
        server.run("streamable-http", host=host, port=port, **transport_kwargs)
        return

    import uvicorn

    app = server.streamable_http_app(host=host, **transport_kwargs)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="debug" if verbose else "warning",
        ssl_certfile=tls_cert,
        ssl_keyfile=tls_key,
    )
