"""A long-lived ExecApprovalStore (the running gateway's exec tool) must pick
up policy edits made by another writer — the TUI policy editor, `flowly
approvals set`, or a hand edit — without a restart. `refresh_if_changed()`
reloads when the file's mtime moves; `execute_command` calls it so the gate
always enforces the current policy.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from flowly.exec import ExecConfig, ExecRequest, execute_command
from flowly.exec.approvals import ExecApprovalStore, _get_approvals_path


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


def _write_policy_file(security: str, ask: str = "off") -> None:
    """Simulate an external writer editing the store file, and push its mtime
    forward so the change is unambiguous regardless of fs mtime resolution."""
    path = _get_approvals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "security": security, "ask": ask, "allowlist": []}),
        encoding="utf-8",
    )
    future = time.time() + 10
    os.utime(path, (future, future))


def test_refresh_picks_up_external_change():
    store = ExecApprovalStore()
    assert store.load().security == "full"

    _write_policy_file("deny")
    store.refresh_if_changed()

    assert store.config.security == "deny"


def test_refresh_is_noop_without_change():
    store = ExecApprovalStore()
    store.load()
    # No external write — refresh must not clobber the loaded config.
    store.refresh_if_changed()
    assert store.config.security == "full"


def test_own_save_is_not_reloaded():
    store = ExecApprovalStore()
    cfg = store.load()
    cfg.security = "allowlist"
    store.save()
    # Our own write must not be seen as an external change.
    store.refresh_if_changed()
    assert store.config.security == "allowlist"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses /bin/sh subprocess")
async def test_execute_command_picks_up_external_policy_change(tmp_path):
    store = ExecApprovalStore()
    store.load()  # full / off → runs freely

    res = await execute_command(
        ExecRequest(command="echo one"), ExecConfig(security="full"), store
    )
    assert res.success is True

    # Another process tightens the policy to deny.
    _write_policy_file("deny")

    res = await execute_command(
        ExecRequest(command="echo two"), ExecConfig(security="full"), store
    )
    assert res.denied is True
    assert res.success is False
