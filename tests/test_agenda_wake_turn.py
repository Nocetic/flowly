"""An agenda wake-up as the agent loop runs it: a hidden system turn in the
conversation it is about, which may decide to say nothing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage

CHAT = "desktop:chat-1"


class _Session:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.extended: list[dict] = []
        self.metadata: dict = {}

    def extend_with_turn_messages(self, **kwargs) -> None:
        self.extended.append(kwargs)


class _Agenda:
    def __init__(self) -> None:
        self.finished: list[dict] = []
        self.owner: list[str] = []

    def turn_finished(self, item_id, **kwargs) -> None:
        self.finished.append({"id": item_id, **kwargs})

    def owner_wrote(self, session_key) -> None:
        self.owner.append(session_key)


def _agent(reply: str | Exception) -> tuple[AgentLoop, _Session, _Agenda, list]:
    agent = object.__new__(AgentLoop)  # bypass heavy __init__
    session = _Session()
    saved: list = []
    pushed: list = []
    agent.sessions = SimpleNamespace(get_or_create=lambda key: session, save=lambda s: saved.append(s))
    agent.tools = {}
    agent.context = SimpleNamespace(build_messages=lambda **kwargs: [])
    agent._memory_manager = None
    agent.model = "test-model"
    agent.agenda = _Agenda()
    agent.compaction = SimpleNamespace(is_silent_reply=lambda text: text.strip().startswith("NO_REPLY"))
    agent._routed_tool_disclosure = lambda *a, **k: SimpleNamespace(definitions=[], all_names=[])
    agent._context_coverage_sidecar = lambda s: ""
    agent._history_with_summary_anchor = lambda s: []
    agent._inject_recent_artifacts_hint = lambda *a, **k: None
    agent._is_live_call_turn = lambda content: False
    agent._provider_continuity_state = lambda s: {}
    agent._commit_provider_continuity = lambda *a, **k: None

    async def _index(*a, **k):
        return None

    agent._index_turn_media = _index

    async def _push(key, text, **kwargs):
        pushed.append((key, text))

    agent._gateway_server = SimpleNamespace(push_session_message=_push)

    async def _loop(**kwargs):
        if isinstance(reply, Exception):
            raise reply
        return reply, [], [], {}, []

    agent._run_llm_tool_loop = _loop
    return agent, session, agent.agenda, pushed


def _wake(item_id: str = "wk_1") -> InboundMessage:
    return InboundMessage(channel="system", sender_id="agenda", chat_id=CHAT,
                          content="[Agenda] look again", metadata={"agenda_item": item_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["[SILENT]", "  [silent]  ", "", "NO_REPLY"])
async def test_a_wake_up_that_decides_to_say_nothing_leaves_no_trace(reply):
    agent, session, agenda, pushed = _agent(reply)
    assert await agent._process_message(_wake()) is None
    assert session.extended == [] and pushed == []
    assert agenda.finished == [{"id": "wk_1", "delivered": False, "text": "", "session_key": CHAT}]


@pytest.mark.asyncio
async def test_a_wake_up_that_writes_is_delivered_in_its_conversation():
    agent, session, agenda, pushed = _agent("Found two laptops under 40k.")
    out = await agent._process_message(_wake())
    assert out.content == "Found two laptops under 40k." and out.channel == "desktop" and out.chat_id == "chat-1"
    assert session.extended[0]["user_content"] == "[System: agenda] [Agenda] look again"
    assert session.extended[0]["user_display_hidden"] is True
    assert pushed == [(CHAT, "Found two laptops under 40k.")]
    assert agenda.finished == [{
        "id": "wk_1", "delivered": True, "text": "Found two laptops under 40k.", "session_key": CHAT,
    }]


@pytest.mark.asyncio
async def test_a_wake_up_turn_that_fails_tells_the_agenda_at_once():
    agent, _session, agenda, _pushed = _agent(RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        await agent._process_message(_wake())
    assert agenda.finished == [{"id": "wk_1", "delivered": False, "text": "", "session_key": CHAT, "failed": True}]


@pytest.mark.asyncio
async def test_other_system_turns_are_unchanged():
    agent, session, agenda, pushed = _agent("[SILENT]")
    msg = InboundMessage(channel="system", sender_id="subagent", chat_id=CHAT, content="[Background task done]")
    out = await agent._process_message(msg)
    assert out is not None and session.extended  # only a wake-up may stay silent
    assert agenda.finished == []


@pytest.mark.asyncio
async def test_a_phone_chat_on_the_gateway_gets_the_message_too():
    agent, _session, _agenda, pushed = _agent("Hello from the agenda")
    msg = InboundMessage(channel="system", sender_id="agenda", chat_id="ios:conv-9",
                         content="look", metadata={"agenda_item": "wk_2"})
    await agent._process_message(msg)
    assert pushed == [("ios:conv-9", "Hello from the agenda")]


class _Quiet:
    """Any method, doing nothing (sync or async as asked)."""

    def __init__(self, awaitable: bool) -> None:
        self._awaitable = awaitable

    def __getattr__(self, name):
        if self._awaitable:
            async def method(*a, **k):
                return None
        else:
            def method(*a, **k):
                return None
        return method


@pytest.mark.asyncio
@pytest.mark.parametrize("channel, owner", [("desktop", True), ("web", True), ("cron", False), ("agenda", False)])
async def test_the_owner_writing_reaches_the_agenda(channel, owner):
    agent, _session, agenda, _pushed = _agent("hi")
    agent.hooks = _Quiet(awaitable=True)
    agent.subagents = _Quiet(awaitable=False)
    agent.tools = _Quiet(awaitable=False)
    agent._started_sessions = set()
    agent.context_epoch = lambda key: 0
    agent._note_turn_usage = lambda *a, **k: None
    agent._schedule_post_turn_compaction = lambda *a, **k: None

    async def inner(msg):
        return None

    agent._process_message_inner = inner
    await agent._process_message(InboundMessage(channel=channel, sender_id="user", chat_id="c1", content="hey"))
    assert agenda.owner == ([f"{channel}:c1"] if owner else [])
