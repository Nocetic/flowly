"""Tests for post-write delta lint on WriteFileTool / EditFileTool.

In-process linters for .py / .json / .yaml / .toml run after write+edit;
pre-existing errors are filtered out so the agent only sees errors this
edit introduced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.agent.tools._lint import check_delta, is_lintable
from flowly.agent.tools.filesystem import EditFileTool, WriteFileTool


# ---------- Pure linter tests ----------


def test_is_lintable_known_extensions():
    assert is_lintable("foo.py")
    assert is_lintable("foo.json")
    assert is_lintable("foo.yaml")
    assert is_lintable("foo.yml")
    assert is_lintable("foo.toml")
    assert not is_lintable("foo.txt")
    assert not is_lintable("foo.md")


def test_check_delta_clean_python():
    assert check_delta("x.py", None, "x = 1\n") is None


def test_check_delta_broken_python_new_file():
    msg = check_delta("x.py", None, "x = (\n")
    assert msg is not None
    assert "Syntax warning" in msg


def test_check_delta_broken_json_new_file():
    msg = check_delta("c.json", None, '{"a": 1')
    assert msg is not None
    assert "JSONDecodeError" in msg


def test_check_delta_pre_existing_error_filtered():
    """Edit on already-broken file with same error → 'pre-existing' message."""
    pre = "x = (\n"
    post = "x = (\ny = 2\n"  # still broken on line 1
    msg = check_delta("x.py", pre, post)
    assert msg is not None
    assert "Pre-existing" in msg


def test_check_delta_new_error_surfaced():
    """Edit introduces a NEW error → warning surfaced."""
    pre = "x = 1\n"
    post = "x = 1\ny = (\n"
    msg = check_delta("x.py", pre, post)
    assert msg is not None
    assert "Syntax warning" in msg


def test_check_delta_unknown_extension_silent():
    assert check_delta("x.txt", None, "anything goes (\n") is None


def test_check_delta_clean_json():
    assert check_delta("c.json", None, '{"a": 1, "b": [2,3]}') is None


def test_check_delta_clean_toml():
    assert check_delta("p.toml", None, '[tool]\nname = "foo"\n') is None


# ---------- Tool integration tests ----------


def _run(coro):
    # asyncio.run fully owns the loop lifecycle (create → set-current → run →
    # close → reset). Unlike a bare new_event_loop()+run_until_complete, it is
    # immune to event-loop state another test in the suite may have left behind
    # (a closed/unset current loop), which previously made these tests fail only
    # when run as part of the full suite.
    return asyncio.run(coro)


def test_write_file_clean_python(tmp_path: Path):
    tool = WriteFileTool(workspace=tmp_path)
    target = tmp_path / "ok.py"
    out = _run(tool.execute(path=str(target), content="x = 1\n"))
    assert "Successfully wrote" in out
    assert "⚠" not in out


def test_write_file_broken_python_emits_warning(tmp_path: Path):
    tool = WriteFileTool(workspace=tmp_path)
    target = tmp_path / "bad.py"
    out = _run(tool.execute(path=str(target), content="x = (\n"))
    assert "Successfully wrote" in out
    assert "⚠" in out
    assert "Syntax warning" in out


def test_write_file_filters_pre_existing(tmp_path: Path):
    tool = WriteFileTool(workspace=tmp_path)
    target = tmp_path / "stale.py"
    target.write_text("x = (\n", encoding="utf-8")  # already broken
    out = _run(tool.execute(path=str(target), content="x = (\ny = 2\n"))
    assert "Pre-existing" in out


def test_edit_file_introduces_new_error(tmp_path: Path):
    target = tmp_path / "good.py"
    target.write_text("x = 1\n", encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    out = _run(tool.execute(path=str(target), old_text="x = 1", new_text="x = ("))
    assert "Successfully edited" in out
    assert "⚠" in out
    assert "Syntax warning" in out


def test_edit_file_clean_json(tmp_path: Path):
    target = tmp_path / "config.json"
    target.write_text('{"a": 1}\n', encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    out = _run(tool.execute(path=str(target), old_text='"a": 1', new_text='"a": 2'))
    assert "Successfully edited" in out
    assert "⚠" not in out


def test_write_file_txt_no_lint_no_warning(tmp_path: Path):
    """Non-lintable extensions don't get warnings even with weird content."""
    tool = WriteFileTool(workspace=tmp_path)
    target = tmp_path / "notes.txt"
    out = _run(tool.execute(path=str(target), content="this ( is fine\n"))
    assert "Successfully wrote" in out
    assert "⚠" not in out
