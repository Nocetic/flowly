"""Tests for runtime cwd resolution (flowly/runtime_cwd.py) and the
service-definition builders that consume it.

All tests stay isolated: they use tmp dirs + monkeypatched env and never
read or write the real ``~/.flowly`` profile.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from flowly.runtime_cwd import (
    FLOWLY_CWD_ENV,
    clear_session_cwd,
    get_session_cwd,
    resolve_runtime_cwd,
    runtime_cwd_context,
    set_session_cwd,
    validate_cwd,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _fake_config(*, default_cwd: str = "", workspace: Path | None = None):
    """Minimal stand-in for flowly.config.schema.Config."""
    ws = workspace or Path.home()
    return SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(cwd=default_cwd)),
        workspace_path=ws,
    )


@pytest.fixture(autouse=True)
def _clean_env_and_registry(monkeypatch):
    """Every test starts with no FLOWLY_CWD and an empty session registry."""
    monkeypatch.delenv(FLOWLY_CWD_ENV, raising=False)
    # The registry is module-global; clear the keys we use defensively.
    for key in ("s1", "s2", "cron:job1"):
        clear_session_cwd(key)
    yield
    for key in ("s1", "s2", "cron:job1"):
        clear_session_cwd(key)


# --------------------------------------------------------------------------
# validate_cwd
# --------------------------------------------------------------------------

def test_validate_cwd_existing_dir(tmp_path):
    assert validate_cwd(str(tmp_path)) == tmp_path.resolve()


def test_validate_cwd_none_and_empty():
    assert validate_cwd(None) is None
    assert validate_cwd("") is None
    assert validate_cwd("   ") is None


def test_validate_cwd_nonexistent(tmp_path):
    assert validate_cwd(str(tmp_path / "nope")) is None


def test_validate_cwd_file_not_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert validate_cwd(str(f)) is None


def test_validate_cwd_require_absolute_rejects_relative():
    assert validate_cwd("some/rel/path", require_absolute=True) is None


def test_validate_cwd_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "proj"
    sub.mkdir()
    assert validate_cwd("~/proj") == sub.resolve()


# --------------------------------------------------------------------------
# resolve_runtime_cwd — priority chain
# --------------------------------------------------------------------------

def test_resolve_empty_falls_back_to_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = _fake_config(workspace=ws)
    assert resolve_runtime_cwd(config=cfg, workspace=ws) == ws


def test_resolve_config_default_cwd(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = _fake_config(default_cwd=str(proj), workspace=ws)
    assert resolve_runtime_cwd(config=cfg, workspace=ws) == proj.resolve()


def test_resolve_env_overrides_config(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; ws.mkdir()
    cfg_dir = tmp_path / "cfg"; cfg_dir.mkdir()
    env_dir = tmp_path / "env"; env_dir.mkdir()
    monkeypatch.setenv(FLOWLY_CWD_ENV, str(env_dir))
    cfg = _fake_config(default_cwd=str(cfg_dir), workspace=ws)
    assert resolve_runtime_cwd(config=cfg, workspace=ws) == env_dir.resolve()


def test_resolve_session_overrides_env_and_config(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; ws.mkdir()
    env_dir = tmp_path / "env"; env_dir.mkdir()
    sess_dir = tmp_path / "sess"; sess_dir.mkdir()
    monkeypatch.setenv(FLOWLY_CWD_ENV, str(env_dir))
    set_session_cwd("s1", str(sess_dir))
    cfg = _fake_config(default_cwd=str(env_dir), workspace=ws)
    assert resolve_runtime_cwd(session_key="s1", config=cfg, workspace=ws) == sess_dir.resolve()


def test_resolve_explicit_overrides_everything(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    sess_dir = tmp_path / "sess"; sess_dir.mkdir()
    explicit = tmp_path / "explicit"; explicit.mkdir()
    set_session_cwd("s1", str(sess_dir))
    cfg = _fake_config(workspace=ws)
    got = resolve_runtime_cwd(
        session_key="s1", explicit=str(explicit), config=cfg, workspace=ws,
    )
    assert got == explicit


def test_resolve_invalid_config_cwd_falls_back_to_workspace(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    cfg = _fake_config(default_cwd=str(tmp_path / "ghost"), workspace=ws)
    assert resolve_runtime_cwd(config=cfg, workspace=ws) == ws


def test_resolve_invalid_env_cwd_falls_back(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv(FLOWLY_CWD_ENV, str(tmp_path / "ghost"))
    cfg = _fake_config(workspace=ws)
    assert resolve_runtime_cwd(config=cfg, workspace=ws) == ws


def test_resolve_stale_session_cwd_ignored(tmp_path, monkeypatch):
    """A session cwd that was valid but later deleted must not win."""
    ws = tmp_path / "ws"; ws.mkdir()
    gone = tmp_path / "gone"; gone.mkdir()
    set_session_cwd("s1", str(gone))
    gone.rmdir()
    cfg = _fake_config(workspace=ws)
    assert resolve_runtime_cwd(session_key="s1", config=cfg, workspace=ws) == ws


def test_resolve_explicit_verbatim_even_if_missing(tmp_path):
    """Explicit override is honoured as-is so the caller owns the error."""
    ws = tmp_path / "ws"; ws.mkdir()
    missing = tmp_path / "missing"
    got = resolve_runtime_cwd(explicit=str(missing), workspace=ws)
    assert got == missing


def test_resolve_no_config_no_workspace_is_home():
    assert resolve_runtime_cwd() == Path.home()


# --------------------------------------------------------------------------
# session registry
# --------------------------------------------------------------------------

def test_set_session_cwd_rejects_missing(tmp_path):
    with pytest.raises(ValueError):
        set_session_cwd("s1", str(tmp_path / "nope"))


def test_set_session_cwd_rejects_relative():
    with pytest.raises(ValueError):
        set_session_cwd("s1", "rel/path")


def test_get_and_clear_session_cwd(tmp_path):
    d = tmp_path / "d"; d.mkdir()
    set_session_cwd("s1", str(d))
    assert get_session_cwd("s1") == d.resolve()
    clear_session_cwd("s1")
    assert get_session_cwd("s1") is None


def test_sessions_are_isolated(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    set_session_cwd("s1", str(a))
    set_session_cwd("s2", str(b))
    assert get_session_cwd("s1") == a.resolve()
    assert get_session_cwd("s2") == b.resolve()


# --------------------------------------------------------------------------
# runtime_cwd_context
# --------------------------------------------------------------------------

def test_context_pins_then_restores(tmp_path):
    d = tmp_path / "d"; d.mkdir()
    assert get_session_cwd("cron:job1") is None
    with runtime_cwd_context("cron:job1", str(d)) as pinned:
        assert pinned == d.resolve()
        assert get_session_cwd("cron:job1") == d.resolve()
    assert get_session_cwd("cron:job1") is None


def test_context_restores_previous(tmp_path):
    prev = tmp_path / "prev"; prev.mkdir()
    new = tmp_path / "new"; new.mkdir()
    set_session_cwd("s1", str(prev))
    with runtime_cwd_context("s1", str(new)) as pinned:
        assert pinned == new.resolve()
        assert get_session_cwd("s1") == new.resolve()
    assert get_session_cwd("s1") == prev.resolve()


def test_context_invalid_cwd_is_noop(tmp_path):
    with runtime_cwd_context("s1", str(tmp_path / "ghost")) as pinned:
        assert pinned is None
        assert get_session_cwd("s1") is None


def test_context_empty_cwd_is_noop():
    with runtime_cwd_context("s1", "") as pinned:
        assert pinned is None
