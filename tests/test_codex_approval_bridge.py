"""Tests for the Codex → Flowly ApprovalManager bridge.

The bridge is the only thing keeping Codex from being neutered by
auto-decline. These tests pin:

  * commandExecution requests route through the manager as a real
    PendingApproval, so the desktop / iOS / Telegram notify channels
    fire the same way they do for the exec tool.
  * fileChange requests get a synthesized "apply_patch" pseudo-command
    so the existing approval UI keeps working without a special-case
    component.
  * permission escalations are declined without bothering the user
    (they picked their profile in ~/.codex/config.toml).
  * Decision mapping is correct: allow-once → accept,
    allow-always → acceptForSession, deny / None → decline.
  * Manager exceptions never propagate — Codex always gets a
    decisive reply so the turn doesn't hang.
"""

from __future__ import annotations

from typing import Any

import pytest

from flowly.codex.approval_bridge import build_codex_approval_callback
from flowly.exec.types import PendingApproval


class _FakeApprovalManager:
    """Records request_and_wait calls and returns scripted decisions."""

    def __init__(self) -> None:
        self.calls: list[PendingApproval] = []
        self.scripted: list[Any] = []
        self.raise_on_next: Exception | None = None

    def script(self, decision: Any) -> None:
        self.scripted.append(decision)

    async def request_and_wait(self, pending: PendingApproval) -> Any:
        self.calls.append(pending)
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        if not self.scripted:
            return "deny"
        return self.scripted.pop(0)


@pytest.mark.asyncio
async def test_command_execution_routes_through_manager() -> None:
    mgr = _FakeApprovalManager()
    mgr.script("allow-once")
    cb = build_codex_approval_callback(
        approval_manager=mgr,
        session_key_getter=lambda: "web:s1",
    )

    reply = await cb({
        "method": "item/commandExecution/requestApproval",
        "params": {
            "command": "/bin/zsh -lc 'pytest -q'",
            "cwd": "/Users/me/repo",
            "reason": "command escapes sandbox",
        },
    })

    # Decision mapped: allow-once → accept.
    assert reply == {"decision": "accept"}

    # One PendingApproval was created and routed through the manager.
    assert len(mgr.calls) == 1
    pending = mgr.calls[0]
    assert pending.request.command == "/bin/zsh -lc 'pytest -q'"
    assert pending.request.cwd == "/Users/me/repo"
    assert pending.session_key == "web:s1"
    # The Codex-supplied reason is surfaced via risk_reasons so the
    # UI can show why approval is needed.
    assert "command escapes sandbox" in pending.risk_reasons


@pytest.mark.asyncio
async def test_file_change_synthesizes_apply_patch_pseudo_command() -> None:
    mgr = _FakeApprovalManager()
    mgr.script("allow-always")
    cb = build_codex_approval_callback(
        approval_manager=mgr,
        session_key_getter=lambda: "web:s2",
    )

    reply = await cb({
        "method": "item/fileChange/requestApproval",
        "params": {
            "reason": "writes outside sandbox root",
            "grantRoot": "/Users/me/repo",
            "itemId": "call_abc",
        },
    })

    # allow-always maps to acceptForSession on the wire.
    assert reply == {"decision": "acceptForSession"}

    assert len(mgr.calls) == 1
    pending = mgr.calls[0]
    # Command label is informative enough for the existing UI.
    assert "apply_patch" in pending.request.command
    assert "/Users/me/repo" in pending.request.command
    # Resolved path carries the grant root so the approval UI can
    # surface where write access is being granted.
    assert pending.resolved_path == "/Users/me/repo"


@pytest.mark.asyncio
async def test_permission_escalation_is_declined_without_prompting() -> None:
    """The user already picked their permission profile in their
    codex config — escalation requests must never trigger a user
    prompt, just decline silently."""
    mgr = _FakeApprovalManager()
    mgr.script("allow-once")  # would be wrong if used
    cb = build_codex_approval_callback(
        approval_manager=mgr,
        session_key_getter=lambda: "web:s3",
    )

    reply = await cb({
        "method": "item/permissions/requestApproval",
        "params": {"reason": "wants /etc access"},
    })

    assert reply == {"decision": "decline"}
    # Critical: the manager was NEVER called — no user prompt fired.
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_unknown_method_declines() -> None:
    mgr = _FakeApprovalManager()
    cb = build_codex_approval_callback(
        approval_manager=mgr,
        session_key_getter=lambda: "web:s4",
    )

    reply = await cb({
        "method": "item/futureSurface/requestApproval",
        "params": {},
    })
    assert reply == {"decision": "decline"}
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_manager_exception_decines_without_propagating() -> None:
    """A manager failure must not raise out of the bridge — it would
    crash the Codex session loop. We decline and let Codex move on."""
    mgr = _FakeApprovalManager()
    mgr.raise_on_next = RuntimeError("notify channels offline")
    cb = build_codex_approval_callback(
        approval_manager=mgr,
        session_key_getter=lambda: "web:s5",
    )

    reply = await cb({
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "ls", "cwd": "/tmp"},
    })
    assert reply == {"decision": "decline"}


@pytest.mark.asyncio
async def test_timeout_falls_through_to_decline() -> None:
    """Manager returns None on timeout. The bridge must convert that
    to a decline so Codex always gets a decisive answer."""
    mgr = _FakeApprovalManager()
    mgr.script(None)  # simulate timeout
    cb = build_codex_approval_callback(
        approval_manager=mgr,
        session_key_getter=lambda: "web:s6",
    )

    reply = await cb({
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "ls"},
    })
    assert reply == {"decision": "decline"}
