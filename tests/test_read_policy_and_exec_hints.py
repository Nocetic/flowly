"""Tests for the UX hardening (2026-06):

1. Read-side path policy opens the user's home tree (read-only) while the
   write policy and the protected/denied security floor stay intact.
2. Denial messages carry actionable next steps instead of a bare
   "Access denied" that locks weak models up.
3. exec exit codes with well-known benign semantics (grep 1, diff 1) are
   annotated as "not an error".
"""

import asyncio
from pathlib import Path

from flowly.agent.tools.filesystem import (
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    _is_path_allowed,
    _is_read_allowed,
    _read_denied_error,
    _write_denied_error,
)
from flowly.agent.tools.shell import _interpret_exit_code


HOME = Path.home()


# ── Read policy: home tree readable, floor intact ─────────────────────────

def test_home_tree_is_readable(tmp_path):
    assert _is_read_allowed((HOME / "some-project" / "main.py").resolve(), tmp_path)
    assert _is_read_allowed(HOME.resolve(), tmp_path)


def test_protected_paths_stay_blocked_for_reads(tmp_path):
    assert not _is_read_allowed((HOME / ".ssh" / "id_rsa").resolve(), tmp_path)
    assert not _is_read_allowed((HOME / ".aws" / "credentials").resolve(), tmp_path)


def test_flowly_auth_artifacts_stay_blocked_for_reads(tmp_path):
    from flowly.profile import get_flowly_home
    assert not _is_read_allowed((get_flowly_home() / "config.json").resolve(), tmp_path)
    assert not _is_read_allowed((get_flowly_home() / "sessions" / "x.jsonl").resolve(), tmp_path)


def test_outside_home_falls_back_to_write_policy(tmp_path):
    assert not _is_read_allowed(Path("/etc/passwd"), tmp_path)
    # Workspace outside home would still be readable via the fallback.
    assert _is_read_allowed(tmp_path / "notes.txt", tmp_path)


def test_write_policy_unchanged_for_home(tmp_path):
    """Opening reads must NOT loosen writes."""
    assert not _is_path_allowed((HOME / "some-project" / "main.py").resolve(), tmp_path)
    assert _is_path_allowed((HOME / "Downloads" / "out.txt").resolve(), tmp_path)


def test_read_file_tool_reads_under_home(tmp_path):
    # tmp_path is outside home on macOS (/private/var/...), so create the
    # probe file in a throwaway dir under home.
    probe_dir = HOME / ".cache" / "flowly-test-read-policy"
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe = probe_dir / "probe.txt"
    probe.write_text("hello from home", encoding="utf-8")
    try:
        tool = ReadFileTool(workspace=tmp_path)
        assert asyncio.run(tool.execute(str(probe))) == "hello from home"
        listing = asyncio.run(ListDirTool(workspace=tmp_path).execute(str(probe_dir)))
        assert "probe.txt" in listing
    finally:
        probe.unlink()
        probe_dir.rmdir()


def test_write_file_tool_still_denied_in_home(tmp_path):
    tool = WriteFileTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(str(HOME / "flowly-test-denied.txt"), "x"))
    assert result.startswith("Error: write access denied")
    assert not (HOME / "flowly-test-denied.txt").exists()


# ── Denial messages carry next steps ──────────────────────────────────────

def test_read_denied_error_protected_says_never():
    msg = _read_denied_error("~/.ssh/id_rsa", (HOME / ".ssh" / "id_rsa").resolve())
    assert "never granted" in msg
    assert "do not retry" in msg


def test_read_denied_error_outside_suggests_exec():
    msg = _read_denied_error("/etc/passwd", Path("/etc/passwd"))
    assert "exec" in msg


def test_write_denied_error_names_writable_locations(tmp_path):
    msg = _write_denied_error("~/x.txt", tmp_path)
    assert str(tmp_path) in msg
    assert "~/Downloads" in msg


# ── Exit-code interpretation ──────────────────────────────────────────────

def test_grep_exit_1_is_benign():
    hint = _interpret_exit_code("grep -r 'needle' src/", 1)
    assert hint and "not an error" in hint


def test_rg_in_pipeline_uses_last_stage():
    # Last stage is head → no benign mapping even though rg leads.
    assert _interpret_exit_code("rg foo | head -5", 1) is None
    # rg as the last stage maps.
    assert _interpret_exit_code("cat file | rg foo", 1) is not None


def test_diff_and_test_exit_1():
    assert "not an error" in _interpret_exit_code("diff a.txt b.txt", 1)
    assert "not an error" in _interpret_exit_code("test -f missing.txt", 1)


def test_env_assignment_prefix_skipped():
    assert _interpret_exit_code("LC_ALL=C grep foo bar.txt", 1) is not None


def test_unknown_commands_get_no_hint():
    assert _interpret_exit_code("python build.py", 1) is None
    assert _interpret_exit_code("grep foo bar.txt", 2) is None  # grep 2 = real error


# ── list_dir survives sandbox-denied entries ──────────────────────────────

def test_list_dir_survives_stat_denied_entry(tmp_path, monkeypatch):
    """The agent's OS sandbox denies metadata on protected entries
    (~/.ssh etc.) — one blocked entry must not kill the whole listing."""
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    (tmp_path / "blocked").mkdir()

    orig = Path.is_dir

    def fake_is_dir(self, **kwargs):
        if self.name == "blocked":
            raise PermissionError(1, "Operation not permitted")
        return orig(self, **kwargs)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    out = asyncio.run(ListDirTool(workspace=tmp_path).execute(str(tmp_path)))
    assert "f ok.txt" in out
    assert "- blocked" in out  # listed, marked inaccessible
    assert not out.startswith("Error")
