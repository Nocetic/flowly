"""Tests for `flowly mcp configure` tool-selection logic (A3)."""

from __future__ import annotations

from flowly.cli.mcp_cmd import _current_selection, _apply_tool_selection
from flowly.config.schema import MCPServerConfig


def _entry(include=None, exclude=None):
    return MCPServerConfig(command="x", tools={"include": include or [], "exclude": exclude or []})


TOOLS = ["echo", "add", "ping"]


# ── current selection ───────────────────────────────────────────────


def test_no_filter_selects_all():
    assert _current_selection(_entry(), TOOLS) == {"echo", "add", "ping"}


def test_include_wins():
    assert _current_selection(_entry(include=["echo"]), TOOLS) == {"echo"}


def test_exclude_applied_when_no_include():
    assert _current_selection(_entry(exclude=["add"]), TOOLS) == {"echo", "ping"}


def test_include_filters_stale_names():
    # An include naming a tool the server no longer exposes is dropped.
    assert _current_selection(_entry(include=["echo", "gone"]), TOOLS) == {"echo"}


# ── apply selection ─────────────────────────────────────────────────


def test_apply_subset_sets_include_in_server_order():
    e = _entry()
    _apply_tool_selection(e, TOOLS, {"ping", "echo"})
    assert e.tools.include == ["echo", "ping"]  # preserves TOOLS order
    assert e.tools.exclude == []


def test_apply_all_clears_filter():
    e = _entry(include=["echo"])
    _apply_tool_selection(e, TOOLS, set(TOOLS))
    assert e.tools.include == []
    assert e.tools.exclude == []


def test_apply_clears_any_existing_exclude():
    e = _entry(exclude=["add"])
    _apply_tool_selection(e, TOOLS, {"echo"})
    assert e.tools.include == ["echo"]
    assert e.tools.exclude == []


def test_roundtrip_current_then_apply_is_stable():
    # Selecting exactly the current selection and re-applying is a no-op
    # on the effective tool set.
    e = _entry(include=["echo", "add"])
    sel = _current_selection(e, TOOLS)
    _apply_tool_selection(e, TOOLS, sel)
    assert _current_selection(e, TOOLS) == {"echo", "add"}
