"""Tests for codex binary resolution under a minimal PATH.

The gateway can run with a minimal PATH (macOS launchd agents start with
``/usr/bin:/bin:/usr/sbin:/sbin``), which excludes Homebrew / npm-global
dirs. ``_resolve_codex_bin`` augments the PATH search with common install
locations so a `codex` that works in the user's shell is still found.
"""

from __future__ import annotations

from flowly.codex.app_server import _CODEX_FALLBACK_DIRS, _resolve_codex_bin


def test_explicit_path_returned_unchanged():
    assert _resolve_codex_bin("/opt/homebrew/bin/codex") == "/opt/homebrew/bin/codex"
    assert _resolve_codex_bin("./codex") == "./codex"


def test_found_on_normal_path(monkeypatch, tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _resolve_codex_bin("codex") == str(fake)


def test_found_via_fallback_dir_when_path_minimal(monkeypatch, tmp_path):
    # Simulate launchd minimal PATH (no Homebrew/npm), and put codex in a
    # dir that is on the fallback list.
    fake_dir = tmp_path / "brewbin"
    fake_dir.mkdir()
    fake = fake_dir / "codex"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        "flowly.codex.app_server._CODEX_FALLBACK_DIRS", (str(fake_dir),)
    )
    assert _resolve_codex_bin("codex") == str(fake)


def test_falls_back_to_bare_name_when_missing(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-dir-xyz")
    monkeypatch.setattr(
        "flowly.codex.app_server._CODEX_FALLBACK_DIRS", ("/also-nonexistent-xyz",)
    )
    # Nothing found → returns the bare name so the spawn raises a clear error.
    assert _resolve_codex_bin("codex") == "codex"


def test_fallback_dirs_include_homebrew_and_npm():
    # Guard against accidental removal of the key macOS locations.
    assert "/opt/homebrew/bin" in _CODEX_FALLBACK_DIRS
    assert any(d.endswith("/.local/bin") for d in _CODEX_FALLBACK_DIRS)
    assert any("npm" in d for d in _CODEX_FALLBACK_DIRS)
