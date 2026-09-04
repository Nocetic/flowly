"""Bounded, cursor-safe pagination for MCP list operations.

MCP servers may paginate tools, resources, and prompts.  Silently reading only
the first page makes a healthy server appear incomplete; blindly following
cursors can hang forever when a server repeats one.  This module centralizes a
bounded implementation that reports truncation explicitly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MCPPageCollection:
    """Items collected from one or more MCP result pages."""

    items: tuple[Any, ...]
    pages: int
    next_cursor: str | None = None
    truncated: bool = False
    reason: str | None = None

    def metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pages": self.pages,
            "truncated": self.truncated,
        }
        if self.next_cursor:
            data["nextCursor"] = self.next_cursor
        if self.reason:
            data["reason"] = self.reason
        return data


def _next_cursor(page: Any) -> str | None:
    value = getattr(page, "next_cursor", None)
    if value is None:
        value = getattr(page, "nextCursor", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def collect_mcp_pages(
    fetch_page: Callable[[str | None], Awaitable[Any]],
    item_field: str,
    *,
    initial_cursor: str | None = None,
    max_pages: int = 100,
    max_items: int = 10_000,
) -> MCPPageCollection:
    """Follow MCP cursors without unbounded work or cursor cycles.

    ``fetch_page`` receives the cursor to send (``None`` for the first page).
    If a safety limit or malformed cursor chain is encountered, already-read
    items are returned with ``truncated=True`` and a machine-readable reason.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if max_items < 1:
        raise ValueError("max_items must be at least 1")

    cursor = str(initial_cursor).strip() if initial_cursor else None
    seen_request_cursors: set[str] = set()
    if cursor:
        seen_request_cursors.add(cursor)
    collected: list[Any] = []

    for page_number in range(1, max_pages + 1):
        page = await fetch_page(cursor)
        page_items = list(getattr(page, item_field, None) or [])
        remaining = max_items - len(collected)
        if len(page_items) > remaining:
            collected.extend(page_items[:remaining])
            return MCPPageCollection(
                items=tuple(collected),
                pages=page_number,
                next_cursor=_next_cursor(page),
                truncated=True,
                reason="max_items",
            )
        collected.extend(page_items)

        next_cursor = _next_cursor(page)
        if next_cursor is None:
            return MCPPageCollection(items=tuple(collected), pages=page_number)
        if next_cursor == cursor or next_cursor in seen_request_cursors:
            return MCPPageCollection(
                items=tuple(collected),
                pages=page_number,
                next_cursor=next_cursor,
                truncated=True,
                reason="cursor_cycle",
            )
        if len(collected) >= max_items:
            return MCPPageCollection(
                items=tuple(collected),
                pages=page_number,
                next_cursor=next_cursor,
                truncated=True,
                reason="max_items",
            )
        if page_number == max_pages:
            return MCPPageCollection(
                items=tuple(collected),
                pages=page_number,
                next_cursor=next_cursor,
                truncated=True,
                reason="max_pages",
            )

        seen_request_cursors.add(next_cursor)
        cursor = next_cursor

    raise AssertionError("unreachable pagination state")
