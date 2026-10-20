"""The computer card: a step the person does on the agent's screen.

Grok-style (2026-10-20): the agent hands the screen over through
browser_tab's handoff action and ends its reply; the person gets a card that
opens the screen, does the step and hands the screen back; the conversation
then gets a new turn of its own in which the agent continues. Real authority
and a fake browser.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.agent.tools import computer_handoff as module
from flowly.agent.tools.browser_tab import BrowserTabTool
from flowly.clarify.manager import get_clarify_manager
from flowly.clarify.wire import clarify_to_wire
from flowly.desktop_runtime.host_screen_run import host_screen_run_scope
from tests.test_screen_gateway_binding import screen_gateway as gateway_fixture


@pytest.fixture(autouse=True)
def _profile_screens_run_on_linux(monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "linux")


screen_gateway = gateway_fixture


@pytest.fixture
def surfaces(monkeypatch):
    """What surfaces are told (the cards), and the turns the agent loop starts."""
    manager = get_clarify_manager()
    cards: list = []
    closed: list = []
    turns: list = []

    async def notify(pending):
        cards.append(pending)

    async def close(clarify_id, reason, session_key):
        closed.append((clarify_id, reason))

    async def continuation(session_key, channel, chat_id, content):
        turns.append((session_key, channel, chat_id, content))

    monkeypatch.setattr(manager, "_notify_callbacks", [notify])
    monkeypatch.setattr(manager, "_close_callbacks", [close])
    monkeypatch.setattr(module, "_POLL_SECONDS", 0.005)
    yield cards, closed, turns, continuation
    module._reset_for_tests()


def tool(gateway, continuation, channel="desktop", chat_id="chat"):
    instance = BrowserTabTool(gateway, screen_continuation=continuation)
    instance.set_context(channel, chat_id)
    return instance


async def _until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_the_agent_hands_the_screen_over_and_continues_once_it_is_handed_back(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    cards, closed, turns, continuation = surfaces
    authority = browser.runtime.authority
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        result = json.loads(await tool(gateway, continuation).execute(
            "handoff", task="Sign in to Amazon (and complete 2FA if asked), then hand back"))
        run.succeeded = True
    # The tool answered at once and the run ended: nothing waits in the turn.
    assert result["outcome"] == "handed_off" and "End your reply" in result["note"]
    assert authority.snapshot()["runId"] is None
    wire = clarify_to_wire(cards[0])
    assert wire["kind"] == "computer" and wire["profileId"] == "default" and wire["sessionKey"] == "desktop:chat"
    assert wire["question"].startswith("Sign in to Amazon")
    assert turns == []
    # The person opens the screen, takes control, signs in, hands back.
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    await asyncio.sleep(0.03)
    assert turns == [] and get_clarify_manager().get_pending(cards[0].id) is not None
    authority.release("human", "generation", human["epoch"])
    await _until(lambda: turns)
    session_key, channel, chat_id, content = turns[0]
    assert (session_key, channel, chat_id) == ("desktop:chat", "desktop", "chat")
    assert content.startswith("[Screen] The person did this on your screen and handed it back: Sign in to Amazon")
    assert "read_page" in content
    assert closed == [(cards[0].id, "answered")]
    assert not get_clarify_manager().list_pending()


async def test_a_screen_the_person_already_holds_continues_on_the_hand_back(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    cards, _, turns, continuation = surfaces
    authority = browser.runtime.authority
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    async with host_screen_run_scope(gateway, "desktop:chat", "run"):
        # The browser refuses while the person has the screen, and says what to do.
        refused = await gateway.send_browser_tool_request("open", "get_page_text", {})
        assert refused["error_code"] == "HUMAN_HAS_CONTROL" and "handoff" in refused["error"]
        result = json.loads(await tool(gateway, continuation).execute("handoff", task="Finish signing in"))
    assert result["outcome"] == "handed_off"
    await _until(lambda: cards)
    authority.release("human", "generation", human["epoch"])
    await _until(lambda: turns)
    assert turns[0][3].startswith("[Screen] The person did this on your screen and handed it back: Finish signing in")


async def test_nobody_in_time_tells_the_agent_once(screen_gateway, surfaces, monkeypatch):
    gateway, _, _ = screen_gateway
    cards, closed, turns, continuation = surfaces
    monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.1)
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        await tool(gateway, continuation).execute("handoff", task="Solve the CAPTCHA")
        run.succeeded = True
    await _until(lambda: turns)
    assert turns[0][3].startswith("[Screen] Nobody did this on your screen in time: Solve the CAPTCHA")
    assert closed == [(cards[0].id, "timeout")]


async def test_a_screen_that_stops_closes_the_card_without_a_turn(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    cards, closed, turns, continuation = surfaces
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        await tool(gateway, continuation).execute("handoff", task="Sign in")
        run.succeeded = True
    browser.runtime.authority.stop("generation")
    await _until(lambda: closed)
    assert closed == [(cards[0].id, "answered")] and turns == []


async def test_a_new_handoff_in_the_conversation_replaces_the_one_before(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    cards, closed, turns, continuation = surfaces
    authority = browser.runtime.authority
    for task in ("Sign in", "Enter the code"):
        async with host_screen_run_scope(gateway, "desktop:chat", f"run-{task}") as run:
            assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
            await tool(gateway, continuation).execute("handoff", task=task)
            run.succeeded = True
    await _until(lambda: closed)
    assert closed == [(cards[0].id, "cancelled")]
    assert [p.question for p in get_clarify_manager().list_pending()] == ["Enter the code"]
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    authority.release("human", "generation", human["epoch"])
    await _until(lambda: turns)
    assert len(turns) == 1 and "Enter the code" in turns[0][3]


async def test_outside_desktop_nothing_is_handed_over(screen_gateway, surfaces):
    gateway, _, _ = screen_gateway
    cards, _, turns, continuation = surfaces
    async with host_screen_run_scope(gateway, "telegram:42", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        result = json.loads(await tool(gateway, continuation, "telegram", "42").execute("handoff", task="Sign in"))
        run.succeeded = True
    assert result["outcome"] == "unavailable" and "Flowly Desktop" in result["note"]
    assert cards == [] and turns == []


async def test_without_a_screen_there_is_no_card(surfaces):
    cards, _, _, continuation = surfaces
    result = json.loads(await BrowserTabTool(None, screen_continuation=continuation).execute("handoff", task="Sign in"))
    assert result["outcome"] == "unavailable" and cards == []


def test_the_handoff_costs_the_browser_tool_one_action_and_one_argument():
    schema = BrowserTabTool(None).parameters
    assert "handoff" in schema["properties"]["action"]["enum"]
    assert len(schema["properties"]["task"]["description"]) < 100


async def test_the_continuation_is_the_conversations_own_hidden_turn(tmp_path, monkeypatch):
    """What the agent loop starts once the screen is handed back: a system
    notice of the screen runs as the conversation's own turn, the notice
    itself stays out of the transcript, and the agent's reply is the next
    assistant message."""
    from typing import Any

    import flowly.profile as profiles
    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.computer_handoff import CONTINUE_NOTICE
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse
    from flowly.session.manager import SessionManager

    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("lovelace")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    seen: list = []

    class Reply(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            seen.append(kwargs["messages"])
            return LLMResponse(content="Signed in. Continuing with the order.")

    loop = AgentLoop(bus=MessageBus(), provider=Reply(), workspace=home / "workspace", main_config=Config(),
                     max_iterations=2, soft_warn_at_iteration=0)
    try:
        submitted: list = []

        async def watching(session_key, notice):
            submitted.append((session_key, notice))
            return False  # nobody watching: the bus carries it

        loop.register_report_turn_submitter("direct", watching)
        published: list = []
        monkeypatch.setattr(loop.bus, "publish_inbound", lambda msg: published.append(msg) or asyncio.sleep(0))
        await loop._continue_screen_task("desktop:chat", "desktop", "chat", CONTINUE_NOTICE.format(task="Sign in"))
        assert submitted[0][1]["sender_id"] == "screen"
        assert published and published[0].sender_id == "screen" and published[0].session_key == "desktop:chat"
        out = await loop._process_message(published[0])
    finally:
        loop.stop()
    assert out is not None and out.content == "Signed in. Continuing with the order."
    assert "[Screen] The person did this on your screen" in str(seen[0])
    shown = [m for m in SessionManager(home / "workspace").get_full_messages("desktop:chat")
             if m.get("role") in ("user", "assistant") and not m.get("tool_calls")]
    assert [m["role"] for m in shown] == ["assistant"]
