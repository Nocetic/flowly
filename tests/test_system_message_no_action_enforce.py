"""Regression: a subagent/board/system announce turn must NOT be action-enforced.

When an async subagent finishes, its result re-enters the parent as a
``channel="system"`` message and ``_process_system_message`` runs a turn to
synthesize it for the user. That turn is a DELIVERY turn — it must never be
classified as an action request, because the announce CONTENT is the subagent's
own report (often containing words like "gönder"/"bildir"/"send") and would
otherwise flip the turn into action-enforce mode, forcing the parent to call a
tool after the work is already done and emitting the misleading
"Tool calls failed, no action was taken."

This test pins ``action_turn=False`` for the system path regardless of how
action-y the announce content looks.
"""

from __future__ import annotations

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage


class _StopLoop(Exception):
    """Short-circuit sentinel so we don't have to stub the post-loop tail."""


class _FakeSession:
    def get_history(self, max_messages: int = 0):
        return []


class _FakeSessions:
    def get_or_create(self, key: str):
        return _FakeSession()


class _FakeContext:
    def build_messages(self, **kwargs):
        return []


def _bare_agent(capture: dict) -> AgentLoop:
    agent = object.__new__(AgentLoop)  # bypass heavy __init__
    agent.sessions = _FakeSessions()
    agent.tools = {}  # .get(...) → None → set_context branches skipped
    agent.context = _FakeContext()
    agent._memory_manager = None
    agent.context_messages = 50
    agent.model = "test-model"
    agent._inject_recent_artifacts_hint = lambda *a, **k: None
    agent._is_live_call_turn = lambda content: False

    async def _capture_loop(**kwargs):
        capture["action_turn"] = kwargs.get("action_turn")
        capture["live_call_turn"] = kwargs.get("live_call_turn")
        raise _StopLoop

    agent._run_llm_tool_loop = _capture_loop
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "announce",
    [
        # Action-y verbs that _is_action_turn would otherwise match.
        "[Background task 'researcher' completed]\n\nTask: araştır\n\n"
        "Result: ... raporu kullanıcıya gönder ve bildir ...",
        "[Background task 'writer' completed]\n\nResult: send this to the user, share it",
        "[2 background tasks completed]\n- researcher\n- writer",
    ],
)
async def test_system_announce_is_never_action_enforced(announce: str) -> None:
    capture: dict = {}
    agent = _bare_agent(capture)
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="web:conv-123",
        content=announce,
    )
    with pytest.raises(_StopLoop):
        await agent._process_system_message(msg)
    assert capture["action_turn"] is False
