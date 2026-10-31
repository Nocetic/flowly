"""`PendingApproval.supports_always` tells surfaces whether offering "Always
allow" is meaningful. An exec command supports it when remembering its
programs makes it run unasked next time, and codex (acceptForSession) does;
tool actions that can never be remembered (send email, create Linear issue,
...) must not — otherwise "Always allow" is a silent no-op that re-asks every
time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.exec.approvals import ExecApprovalStore
from flowly.exec.types import ExecRequest, PendingApproval


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


def _pending(command: str, cwd: str | None = None) -> PendingApproval:
    store = ExecApprovalStore()
    store.load()
    store.config.security, store.config.ask = "allowlist", "on-miss"
    return store.create_pending(ExecRequest(command=command, cwd=cwd))


def test_a_program_that_does_not_read_supports_always():
    pending = _pending("git commit -m x")
    assert pending.supports_always is True
    assert len(pending.remembers) == 1 and pending.remembers[0].endswith("/git")


@pytest.mark.parametrize("command", [
    # Remembering cat or git did nothing here: the variable, the chain, the
    # write redirection or the interpreter asked again the next time.
    'cat "$USER/notes.md"',
    "git commit -m x > log.txt",
    "python3 -c 'print(1)'",
    "definitely-not-a-real-binary-xyz --flag",
    # A dangerous pattern asks every time.
    "git push --force",
])
def test_always_is_not_offered_where_it_would_do_nothing(command):
    pending = _pending(command)
    assert pending.supports_always is False
    assert pending.remembers == ()


def test_always_is_not_offered_in_ask_mode():
    store = ExecApprovalStore()
    store.load()
    store.config.security, store.config.ask = "full", "always"
    assert store.create_pending(ExecRequest(command="git commit -m x")).supports_always is False


def test_always_remembers_every_program_of_a_chain():
    store = ExecApprovalStore()
    store.load()
    store.config.security, store.config.ask = "allowlist", "on-miss"
    pending = store.create_pending(ExecRequest(command="git add -A && git commit -m x; make 2>&1 | tail -3"))
    assert [Path(p).name for p in pending.remembers] == ["git", "make"]

    store.resolve_pending(pending.id, "allow-always")

    assert {entry.pattern for entry in store.config.allowlist} == set(pending.remembers)


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
