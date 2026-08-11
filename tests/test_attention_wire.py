"""One serializer per attention event, shared by every transport.

``exec.approval.requested`` reaches surfaces three ways — gateway WS broadcast,
relay frame via the web channel, and ``exec.approval.list`` on reconnect — and
clarify does the same. Each of those used to build its own dict, which is how
``supportsAlways`` ended up on two of them and ``sessionKey`` on one. These
tests pin the payload so a field added for one surface cannot go missing on
another.

The close events matter for the same reason: a card that outlives its request
eventually fires a decision against an id the manager has already dropped.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from flowly.clarify.types import ClarifyRequest
from flowly.clarify.wire import clarify_closed_to_wire, clarify_to_wire
from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.types import ExecRequest, PendingApproval
from flowly.exec.wire import approval_closed_to_wire, approval_to_wire


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


# ── approval payload ────────────────────────────────────────────────────


def test_approval_wire_carries_every_field():
    pending = PendingApproval(
        id="a1",
        request=ExecRequest(command="rm -rf /tmp/x", cwd="/work"),
        created_at=100.0,
        expires_at=220.0,
        session_key="web:conv1",
        resolved_path="/bin/rm",
        risk_reasons=["Matches dangerous command pattern"],
        supports_always=True,
        kind="exec",
    )

    assert approval_to_wire(pending) == {
        "id": "a1",
        "command": "rm -rf /tmp/x",
        "kind": "exec",
        "sessionKey": "web:conv1",
        "createdAt": 100.0,
        "expiresAt": 220.0,
        "supportsAlways": True,
        "riskReasons": ["Matches dangerous command pattern"],
        "cwd": "/work",
        "resolvedPath": "/bin/rm",
    }


def test_approval_wire_defaults_are_safe():
    """A tool that fills in only the required fields still serializes."""
    pending = PendingApproval(
        id="a2",
        request=ExecRequest(command="Send email to a@b.com"),
        created_at=0.0,
        expires_at=1.0,
    )
    data = approval_to_wire(pending)

    assert data["kind"] == "action"          # not a shell command
    assert data["riskReasons"] == []
    assert data["cwd"] is None
    assert data["resolvedPath"] is None
    assert data["sessionKey"] is None
    assert data["supportsAlways"] is True    # dataclass default (codex needs it)


def test_approval_wire_risk_reasons_is_a_copy():
    """Surfaces must not be able to mutate the manager's live request."""
    reasons = ["Uses command substitution"]
    pending = PendingApproval(
        id="a3",
        request=ExecRequest(command="echo $(id)"),
        created_at=0.0,
        expires_at=1.0,
        risk_reasons=reasons,
    )
    approval_to_wire(pending)["riskReasons"].append("injected")
    assert reasons == ["Uses command substitution"]


@pytest.mark.parametrize("kind", ["exec", "action", "codex"])
def test_approval_wire_preserves_kind(kind):
    pending = PendingApproval(
        id="a4",
        request=ExecRequest(command="x"),
        created_at=0.0,
        expires_at=1.0,
        kind=kind,
    )
    assert approval_to_wire(pending)["kind"] == kind


def test_exec_store_marks_its_approvals_as_exec():
    from flowly.exec.approvals import ExecApprovalStore

    store = ExecApprovalStore()
    store.load()
    pending = store.create_pending(ExecRequest(command="git status"))
    assert approval_to_wire(pending)["kind"] == "exec"


def test_codex_bridge_marks_its_approvals_as_codex():
    from flowly.codex.approval_bridge import _build_pending_for_command

    pending = _build_pending_for_command(
        {"command": "/bin/zsh -lc 'pytest -q'", "reason": "escapes sandbox"},
        "web:conv1",
    )
    data = approval_to_wire(pending)
    assert data["kind"] == "codex"
    # Codex remembers a decision for the rest of its thread (acceptForSession),
    # so "Always" is meaningful there even with no path to allowlist.
    assert data["supportsAlways"] is True


@pytest.mark.asyncio
async def test_tool_approval_is_marked_non_persistable(monkeypatch):
    """An email send can never be remembered — the wire must say so, or the
    surface offers an "Always allow" that silently re-asks next time."""
    import flowly.exec.approval_manager as mgr_mod

    captured: dict = {}

    class _FakeMgr:
        async def request_and_wait(self, pending):
            captured["pending"] = pending
            return "deny"

    monkeypatch.setattr(mgr_mod, "get_approval_manager", lambda: _FakeMgr())

    from flowly.agent.tools.email import EmailTool

    await EmailTool()._require_approval("📧 Send email to a@b.com", session_key="web:1")

    data = approval_to_wire(captured["pending"])
    assert data["supportsAlways"] is False
    assert data["kind"] == "action"


# ── clarify payload ─────────────────────────────────────────────────────


def test_clarify_wire_carries_every_field():
    pending = ClarifyRequest(
        id="c1",
        question="Which environment?",
        choices=["staging", "prod"],
        session_key="web:conv1",
        created_at=10.0,
        expires_at=70.0,
        risk_reasons=["deploy target"],
    )

    assert clarify_to_wire(pending) == {
        "id": "c1",
        "question": "Which environment?",
        "choices": ["staging", "prod"],
        "sessionKey": "web:conv1",
        "createdAt": 10.0,
        "expiresAt": 70.0,
        "riskReasons": ["deploy target"],
    }


def test_clarify_wire_open_ended_keeps_none_choices():
    """``None`` and ``[]`` mean different things to a surface: no buttons at all
    versus an empty button row. Don't let the serializer blur them."""
    pending = ClarifyRequest(id="c2", question="Say what?", choices=None)
    assert clarify_to_wire(pending)["choices"] is None


# ── close payloads ──────────────────────────────────────────────────────


def test_approval_closed_wire():
    assert approval_closed_to_wire("a1", "allow-once", "web:1") == {
        "id": "a1",
        "reason": "allow-once",
        "sessionKey": "web:1",
    }


def test_closed_wire_normalises_missing_session_key():
    """Surfaces filter on sessionKey; ``None`` would compare unequal to the
    empty string one transport already sends."""
    assert approval_closed_to_wire("a1", "timeout", None)["sessionKey"] == ""
    assert clarify_closed_to_wire("c1", "timeout", None)["sessionKey"] == ""


# ── approval close channel ──────────────────────────────────────────────


def _pending(expires_in: float = 5.0) -> PendingApproval:
    now = time.time()
    return PendingApproval(
        id="a-close",
        request=ExecRequest(command="ls"),
        created_at=now,
        expires_at=now + expires_in,
        session_key="web:1",
        kind="exec",
    )


@pytest.mark.asyncio
async def test_close_fires_with_the_decision():
    mgr = ApprovalManager()
    closed: list[tuple[str, str, str]] = []

    async def on_close(approval_id, reason, session_key):
        closed.append((approval_id, reason, session_key))

    mgr.add_close_callback(on_close)
    pending = _pending()

    async def decide():
        await asyncio.sleep(0.01)
        assert mgr.resolve(pending.id, "allow-always") is True

    asyncio.create_task(decide())
    assert await mgr.request_and_wait(pending) == "allow-always"
    assert closed == [("a-close", "allow-always", "web:1")]


@pytest.mark.asyncio
async def test_close_fires_on_timeout():
    mgr = ApprovalManager()
    closed: list[tuple[str, str, str]] = []

    async def on_close(approval_id, reason, session_key):
        closed.append((approval_id, reason, session_key))

    mgr.add_close_callback(on_close)

    assert await mgr.request_and_wait(_pending(expires_in=0.01)) is None
    assert closed == [("a-close", "timeout", "web:1")]


@pytest.mark.asyncio
async def test_close_fires_exactly_once():
    mgr = ApprovalManager()
    calls = 0

    async def on_close(approval_id, reason, session_key):
        nonlocal calls
        calls += 1

    mgr.add_close_callback(on_close)

    async def decide():
        await asyncio.sleep(0.01)
        mgr.resolve("a-close", "deny")
        # A second resolve for the same id is a no-op and must not re-close.
        mgr.resolve("a-close", "allow-once")

    asyncio.create_task(decide())
    await mgr.request_and_wait(_pending())
    assert calls == 1


@pytest.mark.asyncio
async def test_close_survives_a_broken_surface():
    """The agent's decision must not depend on a client staying alive."""
    mgr = ApprovalManager()
    reached: list[str] = []

    async def boom(approval_id, reason, session_key):
        raise RuntimeError("client went away")

    async def healthy(approval_id, reason, session_key):
        reached.append(reason)

    mgr.add_close_callback(boom)
    mgr.add_close_callback(healthy)

    async def decide():
        await asyncio.sleep(0.01)
        mgr.resolve("a-close", "deny")

    asyncio.create_task(decide())
    assert await mgr.request_and_wait(_pending()) == "deny"
    assert reached == ["deny"]


@pytest.mark.asyncio
async def test_cron_short_circuit_does_not_close(monkeypatch):
    """The cron gate answers before any surface is notified, so there is no
    card to retire — firing close there would be noise on a request nobody
    ever saw."""
    import flowly.exec.approval_manager as mgr_mod

    monkeypatch.setattr(
        mgr_mod.ApprovalManager,
        "_cron_mode_decision",
        staticmethod(lambda pending: "deny"),
    )

    mgr = ApprovalManager()
    closed: list[str] = []

    async def on_close(approval_id, reason, session_key):
        closed.append(reason)

    mgr.add_close_callback(on_close)

    assert await mgr.request_and_wait(_pending()) == "deny"
    assert closed == []
