"""A chat that is reopened still shows what the agent is waiting on.

An approval or a clarify question is announced once, as an event. A client
that reloads, restarts or re-enters the chat after that event has only the
re-entry handshake (``chat.inflight``) to learn the agent is still parked, so
the handshake carries the conversation's open prompts in the same shape as the
events.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import flowly.clarify.manager as clarify_module
import flowly.exec.approval_manager as approval_module
from flowly.channels.feature_rpc import chat_inflight
from flowly.clarify.manager import ClarifyManager
from flowly.clarify.types import ClarifyRequest
from flowly.clarify.wire import clarify_to_wire
from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.types import ExecRequest, PendingApproval
from flowly.exec.wire import approval_to_wire


@pytest.fixture
def managers(monkeypatch: pytest.MonkeyPatch):
    approvals, questions = ApprovalManager(), ClarifyManager()
    monkeypatch.setattr(approval_module, "_manager", approvals)
    monkeypatch.setattr(clarify_module, "_manager", questions)
    return approvals, questions


def _approval(session_key: str, approval_id: str, created_at: float) -> PendingApproval:
    return PendingApproval(id=approval_id, request=ExecRequest(command="ls ~"), created_at=created_at,
                           expires_at=time.time() + 30, session_key=session_key, kind="exec")


def _question(session_key: str, question_id: str) -> ClarifyRequest:
    now = time.time()
    return ClarifyRequest(id=question_id, question="Which one?", choices=["A", "B"],
                          session_key=session_key, created_at=now, expires_at=now + 30)


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_reentry_brings_back_the_open_approval_and_question(managers):
    approvals, questions = managers
    older = _approval("desktop:chat", "a-old", 1.0)
    newer = _approval("desktop:chat", "a-new", 2.0)
    elsewhere = _approval("desktop:other", "a-other", 1.5)
    question = _question("desktop:chat", "q1")
    waits = [asyncio.create_task(approvals.request_and_wait(item)) for item in (newer, elsewhere, older)]
    waits.append(asyncio.create_task(questions.request_and_wait(question)))
    await _until(lambda: len(approvals.list_pending()) == 3 and questions.list_pending())

    result = chat_inflight({"sessionKey": "desktop:chat"})

    # Same shape as the live events, oldest first, only this conversation's.
    assert result["approvals"] == [approval_to_wire(older), approval_to_wire(newer)]
    assert result["clarifies"] == [clarify_to_wire(question)]

    for approval_id in ("a-old", "a-new", "a-other"):
        approvals.resolve(approval_id, "deny")
    questions.resolve("q1", "A")
    await asyncio.gather(*waits)
    after = chat_inflight({"sessionKey": "desktop:chat"})
    assert after["approvals"] == [] and after["clarifies"] == []


def test_a_broken_registry_does_not_break_the_handshake(managers, monkeypatch):
    def broken():
        raise RuntimeError("registry offline")

    monkeypatch.setattr(approval_module, "get_approval_manager", broken)
    result = chat_inflight({"sessionKey": "desktop:chat"})
    assert result["approvals"] == [] and result["clarifies"] == []
    assert "inflight" in result
