"""The exec tool must resolve its cwd through runtime_cwd resolution
rather than always running in the workspace.

We capture the ExecRequest instead of actually spawning a subprocess,
and pin FLOWLY_HOME to a tmp dir so the approval store never touches the
real ~/.flowly profile.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import flowly.agent.tools.shell as shell_mod
from flowly.exec import ExecConfig, ExecResult
from flowly.runtime_cwd import clear_session_cwd, set_session_cwd


def _fake_config(*, default_cwd: str = "", workspace: Path | None = None):
    ws = workspace or Path.home()
    return SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(cwd=default_cwd)),
        workspace_path=ws,
    )


@pytest.fixture
def captured_request(monkeypatch, tmp_path):
    """Patch execute_command to capture the request; isolate FLOWLY_HOME."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FLOWLY_CWD", raising=False)
    seen = {}

    async def _fake_execute(request, config, store):
        seen["request"] = request
        return ExecResult(success=True, exit_code=0, stdout="ok")

    monkeypatch.setattr(shell_mod, "execute_command", _fake_execute)
    return seen


@pytest.mark.asyncio
async def test_exec_defaults_to_workspace(captured_request, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    tool = shell_mod.SecureExecTool(
        config=ExecConfig(security="full"),
        working_dir=str(ws),
        main_config=_fake_config(workspace=ws),
    )
    await tool.execute(command="pwd", session_key="s1")
    assert Path(captured_request["request"].cwd) == ws


@pytest.mark.asyncio
async def test_exec_honours_agents_default_cwd(captured_request, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    proj = tmp_path / "proj"; proj.mkdir()
    tool = shell_mod.SecureExecTool(
        config=ExecConfig(security="full"),
        working_dir=str(ws),
        main_config=_fake_config(default_cwd=str(proj), workspace=ws),
    )
    await tool.execute(command="pwd", session_key="s1")
    assert Path(captured_request["request"].cwd) == proj.resolve()


@pytest.mark.asyncio
async def test_exec_session_cwd_wins(captured_request, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    sess = tmp_path / "sess"; sess.mkdir()
    set_session_cwd("s-exec", str(sess))
    try:
        tool = shell_mod.SecureExecTool(
            config=ExecConfig(security="full"),
            working_dir=str(ws),
            main_config=_fake_config(workspace=ws),
        )
        await tool.execute(command="pwd", session_key="s-exec")
        assert Path(captured_request["request"].cwd) == sess.resolve()
    finally:
        clear_session_cwd("s-exec")


@pytest.mark.asyncio
async def test_exec_explicit_working_dir_overrides(captured_request, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    sess = tmp_path / "sess"; sess.mkdir()
    explicit = tmp_path / "explicit"; explicit.mkdir()
    set_session_cwd("s-exec2", str(sess))
    try:
        tool = shell_mod.SecureExecTool(
            config=ExecConfig(security="full"),
            working_dir=str(ws),
            main_config=_fake_config(workspace=ws),
        )
        await tool.execute(
            command="pwd", working_dir=str(explicit), session_key="s-exec2",
        )
        assert Path(captured_request["request"].cwd) == explicit
    finally:
        clear_session_cwd("s-exec2")
