"""A memory flush is housekeeping, not a turn in the conversation.

The flush runs its own LLM turn so the agent can write durable facts to disk
before compaction replaces the history. It used to write that exchange BACK
into the session as a user turn carrying the instruction ("save any durable
facts…") plus the model's reply.

Two consequences, both observed live:

  * The pair reached the display transcript, so the user's chat history grew
    turns they never sent.
  * The model reads that history on every later turn and treats a recent user
    instruction as still standing. A user who replied "thanks" got a file
    written into their memory folder and a report about it — the last
    instruction the model could see told it to save memories.
"""

from __future__ import annotations

import pytest

from flowly.agent.loop import AgentLoop
from flowly.compaction.service import CompactionService
from flowly.compaction.types import CompactionConfig
from flowly.providers.base import LLMResponse
from flowly.session.manager import Session


class _Provider:
    provider_name = "stub"

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content, finish_reason="stop")


class _Tools:
    def get_definitions(self):
        return []

    async def execute(self, name, args):
        return "ok"


class _Context:
    def build_messages(self, **kwargs):
        return [{"role": "system", "content": "sys"}]


class _Sessions:
    def __init__(self):
        self.saves = 0

    def save(self, session):
        self.saves += 1


class _Bus:
    def __init__(self):
        self.published: list = []

    async def publish_outbound(self, msg):
        self.published.append(msg)


class _Harness:
    _run_memory_flush = AgentLoop._run_memory_flush
    _history_with_summary_anchor = AgentLoop._history_with_summary_anchor
    # The flush now asks the route which tools it may advertise and execute,
    # so the harness borrows that resolution rather than stubbing a policy.
    _resolve_toolset_route = AgentLoop._resolve_toolset_route
    _routed_tool_definitions = AgentLoop._routed_tool_definitions

    def __init__(self, reply: str):
        self.provider = _Provider(reply)
        self.tools = _Tools()
        self.context = _Context()
        self.sessions = _Sessions()
        self.bus = _Bus()
        self.model = "m"
        self.context_messages = 100
        self.compaction = CompactionService(
            provider=self.provider, model="m", config=CompactionConfig(),
        )


def _session() -> Session:
    session = Session(key="web:conv-1")
    session.add_message("user", "what changed in the deploy?")
    session.add_message("assistant", "Shard nine restarted twice.")
    return session


async def test_the_flush_leaves_no_trace_in_the_conversation():
    session = _session()
    harness = _Harness("Saved the deploy findings.")
    before = [dict(m) for m in session.messages]

    await harness._run_memory_flush(session, "web", "conv-1")

    assert session.messages == before, (
        "the flush wrote itself into the conversation — the user's transcript "
        "grows turns they never sent, and the model reads its instruction as "
        "still standing on every later turn"
    )


async def test_the_flush_instruction_cannot_become_a_standing_order():
    """The exact live failure: the next ordinary turn must not find a recent
    'save durable facts' instruction sitting in its history."""
    session = _session()
    harness = _Harness("Saved.")

    await harness._run_memory_flush(session, "web", "conv-1")
    session.add_message("user", "thanks")

    history_text = " ".join(
        str(m.get("content", "")) for m in session.get_history(max_messages=50)
    )
    assert "Memory Flush" not in history_text
    assert "durable" not in history_text.lower()


async def test_a_background_flush_never_messages_the_user():
    """Post-turn, nobody is waiting on a reply — so anything it says is the
    agent messaging the user unprompted about its own housekeeping."""
    harness = _Harness("I noted the deploy findings for you.")

    await harness._run_memory_flush(_session(), "web", "conv-1", announce=False)

    assert harness.bus.published == []


async def test_an_in_turn_flush_may_still_speak_up():
    """On the pre-turn path the user IS waiting, and the flush's silent-token
    convention exists so the agent can say something worth saying."""
    harness = _Harness("I noticed your database migration is still open.")

    await harness._run_memory_flush(_session(), "web", "conv-1")

    assert len(harness.bus.published) == 1
    assert "migration" in harness.bus.published[0].content


@pytest.mark.parametrize("reply", ["", "   "])
async def test_a_silent_flush_says_nothing(reply):
    harness = _Harness(reply)

    await harness._run_memory_flush(_session(), "web", "conv-1")

    assert harness.bus.published == []
