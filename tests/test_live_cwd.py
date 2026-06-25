"""Live `cd` tracking: a `cd` in one exec persists to the next exec in the
same session (POSIX). Runs real /bin/sh subprocesses; isolated via tmp
FLOWLY_HOME so the real ~/.flowly is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowly.agent.tools.shell import SecureExecTool
from flowly.exec import ExecConfig, ExecRequest, execute_command
from flowly.exec.approvals import ExecApprovalStore
from flowly.runtime_cwd import clear_session_cwd, get_session_cwd

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="live cwd tracking is POSIX-only in v1"
)


def _cfg(workspace: Path):
    return SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(cwd="")),
        workspace_path=workspace,
    )


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    t = SecureExecTool(
        config=ExecConfig(security="full"),
        working_dir=str(ws),
        main_config=_cfg(ws),
    )
    yield t, ws
    for k in ("s1", "s2"):
        clear_session_cwd(k)


async def _pwd(tool, sk):
    out = await tool.execute(command="pwd", session_key=sk)
    return Path(out.strip().splitlines()[-1])


@pytest.mark.asyncio
async def test_cd_persists_across_execs(tool, tmp_path):
    t, ws = tool
    sub = tmp_path / "sub"
    sub.mkdir()
    await t.execute(command=f"cd {sub}", session_key="s1")
    assert await _pwd(t, "s1") == sub.resolve()


@pytest.mark.asyncio
async def test_relative_cd_resolves_against_live_cwd(tool, tmp_path):
    t, ws = tool
    (tmp_path / "sub" / "deeper").mkdir(parents=True)
    await t.execute(command=f"cd {tmp_path / 'sub'}", session_key="s1")
    await t.execute(command="cd deeper", session_key="s1")
    assert await _pwd(t, "s1") == (tmp_path / "sub" / "deeper").resolve()


@pytest.mark.asyncio
async def test_sessions_are_isolated(tool, tmp_path):
    t, ws = tool
    sub = tmp_path / "sub"
    sub.mkdir()
    await t.execute(command=f"cd {sub}", session_key="s1")
    assert await _pwd(t, "s1") == sub.resolve()
    # s2 never cd'd — still the workspace default.
    assert await _pwd(t, "s2") == ws.resolve()


@pytest.mark.asyncio
async def test_no_cd_stays_in_workspace(tool):
    t, ws = tool
    await t.execute(command="echo hi", session_key="s1")
    assert await _pwd(t, "s1") == ws.resolve()


@pytest.mark.asyncio
async def test_exit_code_preserved_through_wrapper(tool):
    t, ws = tool
    out = await t.execute(command="false", session_key="s1")
    assert "Exit code: 1" in out


@pytest.mark.asyncio
async def test_no_marker_leaks_into_stdout(tool):
    t, ws = tool
    out = await t.execute(command="echo hello", session_key="s1")
    assert out.strip() == "hello"
    assert "flowly-cwd" not in out and "pwd -P" not in out


@pytest.mark.asyncio
async def test_deleted_cwd_recovers_to_workspace(tool, tmp_path):
    t, ws = tool
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    await t.execute(command=f"cd {doomed}", session_key="s1")
    assert await _pwd(t, "s1") == doomed.resolve()
    doomed.rmdir()  # cwd deleted out from under the session
    # Resolver re-validates the stale pin and falls back — no wedge/crash.
    assert await _pwd(t, "s1") == ws.resolve()


@pytest.mark.asyncio
async def test_unsessioned_exec_is_not_tracked(tmp_path, monkeypatch):
    """A request with no session_key is never wrapped and pins nothing."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    sub = tmp_path / "sub"
    sub.mkdir()
    store = ExecApprovalStore()
    store.load()
    res = await execute_command(
        ExecRequest(command=f"cd {sub}; pwd", cwd=str(tmp_path)),
        ExecConfig(security="full"),
        store,
    )
    assert res.success
    # Command output still works...
    assert str(sub.resolve()) in res.stdout or str(sub) in res.stdout
    # ...but nothing was pinned (no session to pin to).
    assert get_session_cwd("") is None
