"""Tests for the ``$FLOWLY_HOME/.env`` loader.

We confirm:

* Quotes (both kinds) are stripped.
* Existing env vars are NOT overridden — shell values always win.
* Comments and malformed lines are skipped silently.
* Idempotent: a second call without ``force=True`` is a no-op.
* Missing file is fine (0 loaded, no error).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.mcp.env_loader import load_flowly_dotenv


@pytest.fixture(autouse=True)
def reset_loader_cache():
    from flowly.mcp import env_loader
    env_loader._loaded_paths.clear()
    yield
    env_loader._loaded_paths.clear()


def test_loads_simple_key_value(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.delenv("MCP_TEST_ALPHA", raising=False)
    (tmp_path / ".env").write_text("MCP_TEST_ALPHA=hello\n")
    added = load_flowly_dotenv()
    assert added == 1
    import os
    assert os.environ["MCP_TEST_ALPHA"] == "hello"


def test_strips_quotes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.delenv("MCP_TEST_QUOTED", raising=False)
    monkeypatch.delenv("MCP_TEST_SQ", raising=False)
    (tmp_path / ".env").write_text(
        'MCP_TEST_QUOTED="quoted value"\n'
        "MCP_TEST_SQ='single quoted'\n",
    )
    load_flowly_dotenv()
    import os
    assert os.environ["MCP_TEST_QUOTED"] == "quoted value"
    assert os.environ["MCP_TEST_SQ"] == "single quoted"


def test_existing_env_wins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setenv("MCP_TEST_PRESET", "shell-wins")
    (tmp_path / ".env").write_text("MCP_TEST_PRESET=file-loses\n")
    load_flowly_dotenv()
    import os
    assert os.environ["MCP_TEST_PRESET"] == "shell-wins"


def test_comments_and_malformed_lines_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.delenv("MCP_TEST_GOOD", raising=False)
    (tmp_path / ".env").write_text(
        "# leading comment\n"
        "MCP_TEST_GOOD=ok\n"
        "no_equals_sign\n"
        "=missing_key\n"
        "\n",
    )
    added = load_flowly_dotenv()
    assert added == 1
    import os
    assert os.environ["MCP_TEST_GOOD"] == "ok"


def test_missing_file_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    # No .env file present.
    assert load_flowly_dotenv() == 0


def test_idempotent_without_force(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.delenv("MCP_TEST_BETA", raising=False)
    (tmp_path / ".env").write_text("MCP_TEST_BETA=v1\n")
    assert load_flowly_dotenv() == 1
    # Second call is short-circuited even if the file changed.
    (tmp_path / ".env").write_text("MCP_TEST_GAMMA=v2\n")
    assert load_flowly_dotenv() == 0
