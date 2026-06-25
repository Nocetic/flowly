"""Tests for stdio command resolution.

Why this matters: Flowly's Nuitka-bundled launcher sanitizes ``PATH``
to a known-safe subset. Without an explicit resolver, bare commands
like ``npx`` fail with ``ENOENT`` and the user sees nothing useful.

We don't exercise the Nuitka path directly — we just verify the
fallback search order against synthetic candidate locations using a
``tmp_path``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from flowly.mcp.stdio_resolver import resolve_stdio_command


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexec true\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_absolute_path_returned_unchanged(tmp_path: Path):
    binary = tmp_path / "bin" / "mytool"
    _make_executable(binary)
    cmd, env = resolve_stdio_command(str(binary), {"PATH": "/usr/bin"})
    assert cmd == str(binary)
    # Directory was prepended so transitive launches see it.
    assert env["PATH"].startswith(str(binary.parent))


def test_resolves_via_path(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    binary = bin_dir / "mycli"
    _make_executable(binary)
    cmd, env = resolve_stdio_command("mycli", {"PATH": str(bin_dir)})
    assert cmd == str(binary)


def test_node_tool_falls_back_to_flowly_home(tmp_path: Path, monkeypatch):
    flowly_home = tmp_path / "flowly"
    npx_path = flowly_home / "node" / "bin" / "npx"
    _make_executable(npx_path)
    monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

    cmd, env = resolve_stdio_command("npx", {"PATH": "/nonexistent/path"})
    assert cmd == str(npx_path)
    assert env["PATH"].startswith(str(npx_path.parent))


def test_unresolvable_command_passes_through(tmp_path: Path):
    cmd, env = resolve_stdio_command(
        "this-command-really-does-not-exist-xyz",
        {"PATH": str(tmp_path)},
    )
    # We do NOT raise — the spawn call surfaces a clear FileNotFoundError
    # with the unresolved name in the message.
    assert cmd == "this-command-really-does-not-exist-xyz"
