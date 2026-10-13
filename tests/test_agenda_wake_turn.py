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
        pushed.append((key, text, kwargs) if kwargs else (key, text))

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
@pytest.mark.parametrize("reply", [
    "[SILENT]", "  [silent]  ", "", "NO_REPLY",
    # Without [SEND] nothing is sent: a remark about having nothing to say
    # must never reach the owner as a message.
    "Nothing new to report since yesterday.",
    "[SEND]", "[SEND] [SILENT]",
])
async def test_a_wake_up_that_decides_to_say_nothing_leaves_no_trace(reply):
    agent, session, agenda, pushed = _agent(reply)
    assert await agent._process_message(_wake()) is None
    assert session.extended == [] and pushed == []
    assert agenda.finished == [{"id": "wk_1", "delivered": False, "text": "", "session_key": CHAT}]


@pytest.mark.asyncio
async def test_a_wake_up_that_writes_is_delivered_in_its_conversation():
    agent, session, agenda, pushed = _agent("[SEND] Found two laptops under 40k.")
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
    agent, _session, _agenda, pushed = _agent("[send]  Hello from the agenda")
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


@pytest.mark.asyncio
async def test_a_wake_up_runs_on_the_agenda_route_and_streams_nothing():
    agent, _session, _agenda, _pushed = _agent("[SILENT]")
    seen: dict = {}

    def disclosure(platform, **kwargs):
        seen["disclosed_for"] = platform
        return SimpleNamespace(definitions=[], all_names=[])

    async def loop(**kwargs):
        seen.update(kwargs)
        return "[SILENT]", [], [], {}, []

    agent._routed_tool_disclosure = disclosure
    agent._run_llm_tool_loop = loop
    await agent._process_message(_wake())
    assert seen["disclosed_for"] == "agenda" and seen["tool_platform"] == "agenda"
    # No live iteration events: a silent wake-up must leave no tool steps behind.
    assert seen["outbound_channel"] == "" and seen["outbound_chat_id"] == ""


@pytest.mark.asyncio
async def test_a_wake_up_does_not_hold_up_other_system_turns():
    agent, _session, _agenda, _pushed = _agent("[SILENT]")
    spawned: list = []
    agent._spawn_concurrent_turn = lambda msg: spawned.append(msg)

    async def sequential(msg):
        raise AssertionError("an agenda wake-up must not run inline")

    agent._process_turn = sequential
    await agent._dispatch_inbound(_wake())
    assert [m.metadata["agenda_item"] for m in spawned] == ["wk_1"]


def test_the_agenda_route_only_looks_things_up_and_plans():
    from flowly.agent.tools.routing import resolve_toolset_filters

    enabled, denied = resolve_toolset_filters("agenda")
    assert enabled == {"web", "memory", "sessions", "agenda"}
    assert {"delivery", "interactive", "scheduling"} <= denied
    # Routing switched off in config still keeps the ceiling here.
    assert resolve_toolset_filters("agenda", enabled=False)[0] == enabled
    # A configured allowlist narrows it, never widens it.
    narrowed, _ = resolve_toolset_filters("agenda", platform_toolsets={"*": ["web", "execution"]})
    assert narrowed == {"web"}
    # Other surfaces are untouched.
    assert resolve_toolset_filters("desktop")[0] is None


def test_the_dreamer_never_learns_from_the_daily_review():
    from flowly.memory.dreamer import is_automation_session

    assert is_automation_session("agenda:review")
    assert not is_automation_session("desktop:chat-1")



@pytest.mark.asyncio
async def test_a_check_in_about_a_goal_carries_its_card():
    from flowly.objectives.store import get_objective_store
    from flowly.profile import get_flowly_home

    (get_flowly_home() / "objectives" / "objectives.json").unlink(missing_ok=True)
    goal = get_objective_store().create(title="A month of physics", category="learning", plan=["Kinematics"])
    agent, session, _agenda, pushed = _agent("[SEND] How did kinematics go?")
    msg = InboundMessage(channel="system", sender_id="agenda", chat_id=CHAT, content="[Agenda] look again",
                         metadata={"agenda_item": "wk_9", "agenda_goal": goal["id"]})
    out = await agent._process_message(msg)
    [card] = session.extended[0]["goal_cards"]
    assert card["id"] == goal["id"] and card["event"] == "checkin" and card["title"] == "A month of physics"
    assert pushed == [(CHAT, "How did kinematics go?", {"metadata": {"goalCards": [card]}})]
    assert out.metadata["goalCards"] == [card]
