"""Audit log reader tests.

Cover the JSONL → entries pipeline, filters (tool / status / search),
pagination and edge cases (missing files, malformed lines, empty folders).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.audit.reader import (
    clear_audit_logs,
    get_stats,
    read_entries,
)


def _write(audit_dir: Path, date: str, lines: list[dict]) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{date}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def _tool_entry(tool: str, success: bool = True, **extra) -> dict:
    return {
        "type": "tool_call",
        "ts": "2026-04-25T10:00:00+00:00",
        "session": "sess_1",
        "tool": tool,
        "args": {},
        "result_snippet": "ok",
        "duration_ms": 12,
        "success": success,
        **extra,
    }


# ── Basic reads ──────────────────────────────────────────────────────────


def test_empty_dir_returns_empty(tmp_path: Path):
    result = read_entries(tmp_path / "audit")
    assert result == {"entries": [], "total": 0, "has_more": False, "next_offset": None}


def test_missing_dir_is_safe(tmp_path: Path):
    result = read_entries(tmp_path / "does-not-exist")
    assert result["entries"] == []
    assert result["total"] == 0


def test_reads_single_file_newest_first(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [
        _tool_entry("write_file", ts="2026-04-25T08:00:00+00:00"),
        _tool_entry("artifact.export", ts="2026-04-25T09:00:00+00:00"),
        _tool_entry("exec", ts="2026-04-25T10:00:00+00:00"),
    ])
    result = read_entries(audit_dir)
    assert len(result["entries"]) == 3
    # Newest line first (file is reversed).
    assert result["entries"][0]["tool"] == "exec"
    assert result["entries"][1]["tool"] == "artifact.export"
    assert result["entries"][2]["tool"] == "write_file"
    assert result["total"] == 3


def test_reads_multiple_files_newest_first(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-23", [_tool_entry("a")])
    _write(audit_dir, "2026-04-25", [_tool_entry("c")])
    _write(audit_dir, "2026-04-24", [_tool_entry("b")])
    result = read_entries(audit_dir)
    tools = [e["tool"] for e in result["entries"]]
    # 25 first (newest), then 24, then 23.
    assert tools == ["c", "b", "a"]


def test_date_filter_scopes_to_one_file(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-24", [_tool_entry("yesterday")])
    _write(audit_dir, "2026-04-25", [_tool_entry("today")])
    result = read_entries(audit_dir, date="2026-04-25")
    assert [e["tool"] for e in result["entries"]] == ["today"]


def test_invalid_date_returns_empty(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [_tool_entry("today")])
    # Malformed date string is rejected silently.
    result = read_entries(audit_dir, date="garbage")
    assert result["entries"] == []
    assert result["total"] == 0


# ── Filters ──────────────────────────────────────────────────────────────


def test_tool_filter(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [
        _tool_entry("exec"),
        _tool_entry("write_file"),
        _tool_entry("exec"),
    ])
    result = read_entries(audit_dir, tool="exec")
    assert result["total"] == 2
    assert all(e["tool"] == "exec" for e in result["entries"])


def test_status_filter_success(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [
        _tool_entry("exec", success=True),
        _tool_entry("exec", success=False),
        _tool_entry("write_file", success=True),
    ])
    result = read_entries(audit_dir, status="success")
    assert result["total"] == 2


def test_status_filter_error(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [
        _tool_entry("exec", success=True),
        _tool_entry("exec", success=False),
    ])
    result = read_entries(audit_dir, status="error")
    assert result["total"] == 1
    assert result["entries"][0]["success"] is False


def test_search_substring_match(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [
        _tool_entry("exec", result_snippet="copied to ~/Downloads/foo.md"),
        _tool_entry("exec", result_snippet="rm -rf /tmp/cache"),
        _tool_entry("write_file", args={"path": "~/Downloads/bar.md"}),
    ])
    result = read_entries(audit_dir, search="downloads")
    assert result["total"] == 2


def test_filter_by_event_type(tmp_path: Path):
    """LLM calls and key rotations don't have a 'tool' field — match by type."""
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [
        _tool_entry("exec"),
        {"type": "llm_call", "ts": "x", "model": "haiku", "prompt_tokens": 10, "completion_tokens": 5},
        {"type": "key_rotation", "ts": "x", "provider": "openrouter"},
    ])
    result = read_entries(audit_dir, tool="llm_call")
    assert result["total"] == 1
    assert result["entries"][0]["model"] == "haiku"


# ── Pagination ───────────────────────────────────────────────────────────


def test_pagination_limit_offset(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [_tool_entry(f"t{i}") for i in range(10)])

    page1 = read_entries(audit_dir, limit=4, offset=0)
    assert len(page1["entries"]) == 4
    assert page1["has_more"] is True
    assert page1["next_offset"] == 4

    page2 = read_entries(audit_dir, limit=4, offset=4)
    assert len(page2["entries"]) == 4
    assert page2["has_more"] is True

    page3 = read_entries(audit_dir, limit=4, offset=8)
    assert len(page3["entries"]) == 2
    assert page3["has_more"] is False
    assert page3["next_offset"] is None


def test_limit_clamped(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [_tool_entry(f"t{i}") for i in range(3)])
    # limit=0 → clamped to 1; limit=10000 → clamped to 500.
    r = read_entries(audit_dir, limit=0)
    assert len(r["entries"]) == 1
    r = read_entries(audit_dir, limit=10_000)
    # We have 3 entries, so 3 returned even though limit cap is 500.
    assert len(r["entries"]) == 3


def test_negative_offset_clamped_to_zero(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [_tool_entry("t")])
    r = read_entries(audit_dir, offset=-5)
    assert len(r["entries"]) == 1


# ── Robustness ───────────────────────────────────────────────────────────


def test_malformed_lines_skipped(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    path = audit_dir / "2026-04-25.jsonl"
    with path.open("w") as f:
        f.write(json.dumps(_tool_entry("good1")) + "\n")
        f.write("{ this is not json\n")  # malformed
        f.write("\n")  # blank line
        f.write(json.dumps(_tool_entry("good2")) + "\n")

    result = read_entries(audit_dir)
    tools = [e["tool"] for e in result["entries"]]
    assert sorted(tools) == ["good1", "good2"]


def test_non_jsonl_files_ignored(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "README.md").write_text("hi")
    _write(audit_dir, "2026-04-25", [_tool_entry("ok")])
    result = read_entries(audit_dir)
    assert len(result["entries"]) == 1


def test_entries_carry_source_date(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-25", [_tool_entry("today")])
    result = read_entries(audit_dir)
    assert result["entries"][0]["_date"] == "2026-04-25"


# ── Stats + clear ────────────────────────────────────────────────────────


def test_get_stats_empty(tmp_path: Path):
    s = get_stats(tmp_path / "audit")
    assert s == {"files": 0, "total_bytes": 0, "oldest_date": None, "newest_date": None}


def test_get_stats_with_files(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-23", [_tool_entry("a")])
    _write(audit_dir, "2026-04-25", [_tool_entry("c")])
    _write(audit_dir, "2026-04-24", [_tool_entry("b")])

    s = get_stats(audit_dir)
    assert s["files"] == 3
    assert s["total_bytes"] > 0
    assert s["oldest_date"] == "2026-04-23"
    assert s["newest_date"] == "2026-04-25"


def test_clear_removes_all_audit_files(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    _write(audit_dir, "2026-04-23", [_tool_entry("a")])
    _write(audit_dir, "2026-04-25", [_tool_entry("c")])
    (audit_dir / "README.md").write_text("keep")

    result = clear_audit_logs(audit_dir)
    assert result["deleted_files"] == 2
    assert (audit_dir / "README.md").exists()  # non-audit kept
    assert not (audit_dir / "2026-04-23.jsonl").exists()
    assert not (audit_dir / "2026-04-25.jsonl").exists()


def test_clear_empty_dir_no_op(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    result = clear_audit_logs(audit_dir)
    assert result == {"deleted_files": 0, "deleted_bytes": 0}
