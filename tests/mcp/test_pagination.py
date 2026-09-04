"""Cursor completeness and safety bounds for MCP list operations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from flowly.mcp.client import MCPServerTask
from flowly.mcp.pagination import collect_mcp_pages


def test_collects_all_pages_in_order() -> None:
    calls: list[str | None] = []
    pages = {
        None: SimpleNamespace(tools=["a", "b"], nextCursor="two"),
        "two": SimpleNamespace(tools=["c"], next_cursor=None),
    }

    async def _run():
        async def _fetch(cursor):
            calls.append(cursor)
            return pages[cursor]

        return await collect_mcp_pages(_fetch, "tools")

    result = asyncio.run(_run())
    assert result.items == ("a", "b", "c")
    assert result.pages == 2
    assert result.truncated is False
    assert calls == [None, "two"]


def test_cursor_cycle_is_reported_without_hanging() -> None:
    async def _run():
        async def _fetch(cursor):
            return SimpleNamespace(resources=[cursor or "first"], nextCursor="repeat")

        return await collect_mcp_pages(_fetch, "resources", max_pages=100)

    result = asyncio.run(_run())
    assert result.items == ("first", "repeat")
    assert result.truncated is True
    assert result.reason == "cursor_cycle"
    assert result.next_cursor == "repeat"


def test_item_and_page_limits_are_explicit() -> None:
    async def _items():
        async def _fetch(_cursor):
            return SimpleNamespace(prompts=[1, 2, 3], nextCursor="more")

        return await collect_mcp_pages(_fetch, "prompts", max_items=2)

    item_limited = asyncio.run(_items())
    assert item_limited.items == (1, 2)
    assert item_limited.reason == "max_items"

    async def _pages():
        async def _fetch(cursor):
            number = int(cursor or "0")
            return SimpleNamespace(tools=[number], nextCursor=str(number + 1))

        return await collect_mcp_pages(_fetch, "tools", max_pages=2)

    page_limited = asyncio.run(_pages())
    assert page_limited.items == (0, 1)
    assert page_limited.reason == "max_pages"
    assert page_limited.next_cursor == "2"


def test_server_discovery_uses_every_tool_page() -> None:
    class _Session:
        async def list_tools(self, *, cursor=None):
            if cursor is None:
                return SimpleNamespace(tools=["one"], nextCursor="next")
            assert cursor == "next"
            return SimpleNamespace(tools=["two"], nextCursor=None)

    async def _run():
        task = MCPServerTask("paged")
        task.session = _Session()
        await task._discover()
        return task

    task = asyncio.run(_run())
    assert task.tools == ["one", "two"]
    assert task.health_snapshot()["toolPagination"] == {
        "pages": 2,
        "truncated": False,
    }


def test_invalid_bounds_fail_before_network_work() -> None:
    called = False

    async def _run():
        async def _fetch(_cursor):
            nonlocal called
            called = True
            return SimpleNamespace(tools=[])

        await collect_mcp_pages(_fetch, "tools", max_pages=0)

    try:
        asyncio.run(_run())
    except ValueError as exc:
        assert "max_pages" in str(exc)
    else:
        raise AssertionError("invalid pagination bounds were accepted")
    assert called is False
