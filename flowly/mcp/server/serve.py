"""FastMCP server wiring for ``flowly mcp serve``.

Registers the read-plane tools (always) and, when ``allow_writes`` is set,
the gateway-backed write tools (Faz 3c). Runs on stdio.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


logger = logging.getLogger(__name__)


_MCP_SERVER_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
    _MCP_SERVER_AVAILABLE = True
except ImportError:
    pass


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def create_server(*, allow_writes: bool = False) -> Any:
    """Build the Flowly FastMCP server with read (and optional write) tools."""
    if not _MCP_SERVER_AVAILABLE:
        raise ImportError(
            "MCP server mode requires the 'mcp' package. "
            f"Install with: {sys.executable} -m pip install 'mcp'"
        )

    from flowly.mcp.server.readplane import (
        get_session_reader,
        channels_list as _channels_list,
    )

    mcp = FastMCP(
        "flowly",
        instructions=(
            "Flowly conversation bridge. Read conversation history across "
            "connected channels (Telegram, Discord, Slack, WhatsApp, web, "
            "email, Teams). Session keys are 'channel:chat_id'."
        ),
    )
    reader = get_session_reader()

    # -- read plane -----------------------------------------------------

    @mcp.tool()
    def conversations_list(
        platform: str | None = None, limit: int = 50, search: str | None = None,
    ) -> str:
        """List Flowly conversations (most recent first).

        Args:
            platform: filter by channel (telegram, discord, slack, ...)
            limit: max conversations (default 50)
            search: filter by text in the session key or preview
        """
        return _dumps(reader.conversations_list(platform, limit, search))

    @mcp.tool()
    def conversation_get(session_key: str) -> str:
        """Get metadata for one conversation by its 'channel:chat_id' key."""
        return _dumps(reader.conversation_get(session_key))

    @mcp.tool()
    def messages_read(session_key: str, limit: int = 50) -> str:
        """Read recent user/assistant messages from a conversation.

        Args:
            session_key: the 'channel:chat_id' key from conversations_list
            limit: max messages, most recent (default 50)
        """
        return _dumps(reader.messages_read(session_key, limit))

    @mcp.tool()
    def messages_search(query: str, limit: int = 20) -> str:
        """Full-text search across all conversations (FTS5).

        Args:
            query: search text
            limit: max hits (default 20)
        """
        return _dumps(reader.messages_search(query, limit))

    @mcp.tool()
    def channels_list(platform: str | None = None) -> str:
        """List configured channels and whether each is enabled."""
        return _dumps(_channels_list(platform))

    # -- write plane (Faz 3c) -------------------------------------------
    if allow_writes:
        try:
            from flowly.mcp.server.writeplane import register_write_tools
            register_write_tools(mcp, _dumps)
        except ImportError:
            logger.warning(
                "MCP serve: write tools requested but write plane is "
                "unavailable in this build; serving read-only.",
            )

    return mcp


def run_server(*, allow_writes: bool = False, verbose: bool = False) -> None:
    """Create and run the Flowly MCP server on stdio."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING, stream=sys.stderr,
    )
    server = create_server(allow_writes=allow_writes)
    server.run("stdio")
