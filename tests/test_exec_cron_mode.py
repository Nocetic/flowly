"""Cron approval policy for the exec tool.

`tools.exec.cron_mode` decides what happens when a scheduled (cron) run hits a
command that would normally need interactive approval — no human is present to
click. The single gate lives in ApprovalManager._cron_mode_decision, which
reads the real `config.json` value. The executor must route through it rather
than short-circuiting with its own (stale) copy.

Regression: the executor used to evaluate `getattr(config, "cron_mode", ...)`
against the runtime ExecConfig, which has no such field, so it always read
"deny" — making `cron_mode="approve"` a no-op and denying every scheduled
command. These tests pin the behaviour to the config.json value.
"""

from __future__ import annotations

import json
import sys

import pytest

from flowly.cron.context import cron_context
from flowly.exec import ExecConfig, ExecRequest, execute_command
from flowly.exec.approval_manager import get_approval_manager
from flowly.exec.approvals import ExecApprovalStore

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="uses /bin/sh subprocess"
)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Isolated store wired to the real ApprovalManager, with ask=always so
    every command requires approval (forcing the cron gate to decide)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    s = ExecApprovalStore()
    cfg = s.load()
    cfg.security = "full"
    cfg.ask = "always"
    s.save()
    s.set_approval_callback(get_approval_manager().request_and_wait)
    return s


def _write_cron_mode(mode):
    from flowly.profile import get_flowly_home

    path = get_flowly_home() / "config.json"
    path.write_text(
        json.dumps({"tools": {"exec": {"enabled": True, "cron_mode": mode}}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_cron_mode_approve_runs_command(store, tmp_path):
    _write_cron_mode("approve")

    with cron_context():
        res = await execute_command(
            ExecRequest(command="echo cron-ok", cwd=str(tmp_path)),
            ExecConfig(security="full"),
            store,
        )

    assert res.denied is False
    assert res.success is True
    assert "cron-ok" in res.stdout


@pytest.mark.asyncio
async def test_cron_mode_deny_blocks_command(store, tmp_path):
    _write_cron_mode("deny")

    with cron_context():
        res = await execute_command(
            ExecRequest(command="echo nope", cwd=str(tmp_path)),
            ExecConfig(security="full"),
            store,
        )

    assert res.denied is True
    assert res.success is False
