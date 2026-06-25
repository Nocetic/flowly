"""`PendingApproval.supports_always` tells surfaces whether offering "Always
allow" is meaningful. Exec commands we can allowlist (resolved_path known) and
codex (acceptForSession) support it; tool actions that can never be remembered
(send email, create Linear issue, ...) must not — otherwise "Always allow" is a
silent no-op that re-asks every time.
"""

from __future__ import annotations

import pytest

from flowly.exec.approvals import ExecApprovalStore
from flowly.exec.types import ExecRequest, PendingApproval


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize(
    "command",
    ["git status", "ls -la", "echo hi | cat", "definitely-not-a-real-binary-xyz"],
)
def test_create_pending_tracks_resolved_path(command):
    store = ExecApprovalStore()
    store.load()
    pending = store.create_pending(ExecRequest(command=command))
    # Invariant: we offer "always" exactly when we have a path to allowlist.
    assert pending.supports_always is (pending.resolved_path is not None)


def test_resolvable_command_supports_always():
    store = ExecApprovalStore()
    store.load()
    pending = store.create_pending(ExecRequest(command="git status"))
    assert pending.resolved_path is not None
    assert pending.supports_always is True


def test_unresolvable_command_does_not_support_always():
    store = ExecApprovalStore()
    store.load()
    pending = store.create_pending(
        ExecRequest(command="definitely-not-a-real-binary-xyz --flag")
    )
    assert pending.resolved_path is None
    assert pending.supports_always is False


def test_default_pending_supports_always():
    # Default True keeps codex (acceptForSession) and plain exec working.
    p = PendingApproval(id="x", request=ExecRequest(command="x"), created_at=0, expires_at=0)
    assert p.supports_always is True


@pytest.mark.asyncio
async def test_email_approval_is_not_persistable(monkeypatch):
    """The email tool's real approval path must mark the request non-always."""
    import flowly.exec.approval_manager as mgr_mod

    captured = {}

    class _FakeMgr:
        async def request_and_wait(self, pending):
            captured["pending"] = pending
            return "deny"

    monkeypatch.setattr(mgr_mod, "get_approval_manager", lambda: _FakeMgr())

    from flowly.agent.tools.email import EmailTool

    tool = EmailTool()
    ok = await tool._require_approval("📧 Send email to a@b.com", session_key="web:1")

    assert ok is False  # we returned "deny"
    assert captured["pending"].supports_always is False
