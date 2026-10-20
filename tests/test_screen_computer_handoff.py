"""The computer card: a step the person does on the agent's screen.

The step is task state (2026-10-20): the agent hands the screen over through
browser_tab's handoff action and ends its reply; the person gets a card that
opens the screen, does the step and hands the screen back; the conversation
then gets a new turn of its own in which the agent continues. The step is a
durable record: it waits across turns, a stopped screen and a restarted
gateway, the agent can withdraw it, a later step supersedes it, and every end
reaches the card as its state. Real authority, real store, a fake browser.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import flowly.clarify.manager as clarify_module
from flowly.agent.tools import computer_handoff as module
from flowly.agent.tools.browser_tab import BrowserTabTool
from flowly.clarify.manager import ClarifyManager, get_clarify_manager
from flowly.clarify.wire import clarify_to_wire
from flowly.desktop_runtime.host_screen_run import host_screen_run_scope
from flowly.desktop_runtime.screen_steps import ScreenSteps
from tests.test_screen_gateway_binding import screen_gateway as gateway_fixture


@pytest.fixture(autouse=True)
def _profile_screens_run_on_linux(monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "linux")


screen_gateway = gateway_fixture


class Surfaces:
    """What surfaces are told (the cards, the closes), and the turns the loop starts."""

    def __init__(self, monkeypatch):
        self.cards: list = []
        self.closed: list = []
        self.turns: list = []
        self._monkeypatch = monkeypatch
        self.attach(get_clarify_manager())

    def attach(self, manager: ClarifyManager) -> None:
        async def notify(pending):
            self.cards.append(pending)

        async def close(clarify_id, reason, session_key):
            self.closed.append((clarify_id, reason))

        self._monkeypatch.setattr(manager, "_notify_callbacks", [notify])
        self._monkeypatch.setattr(manager, "_close_callbacks", [close])

    async def continuation(self, session_key, channel, chat_id, content):
        self.turns.append((session_key, channel, chat_id, content))

    def restart(self) -> ClarifyManager:
        """The gateway process died: watchers and the in-memory manager with it."""
        module._reset_for_tests()
        fresh = ClarifyManager()
        self._monkeypatch.setattr(clarify_module, "_manager", fresh)
        self.attach(fresh)
        return fresh


@pytest.fixture
def surfaces(monkeypatch):
    monkeypatch.setattr(module, "_POLL_SECONDS", 0.005)
    yield Surfaces(monkeypatch)
    module._reset_for_tests()


def tool(gateway, continuation, channel="desktop", chat_id="chat"):
    instance = BrowserTabTool(gateway, screen_continuation=continuation)
    instance.set_context(channel, chat_id)
    return instance


async def _until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


async def hand_over(gateway, surfaces, task, run="run"):
    async with host_screen_run_scope(gateway, "desktop:chat", run) as scope:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        result = json.loads(await tool(gateway, surfaces.continuation).execute("handoff", task=task))
        scope.succeeded = True
    return result


def take_and_hand_back(authority, generation="generation"):
    human = authority.acquire("human", generation, authority.snapshot()["epoch"])
    authority.release("human", generation, human["epoch"])


async def test_the_agent_hands_the_screen_over_and_continues_once_it_is_handed_back(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    authority = browser.runtime.authority
    result = await hand_over(gateway, surfaces, "Sign in to Amazon (and complete 2FA if asked), then hand back")
    # The tool answered at once and the run ended: nothing waits in the turn.
    assert result["outcome"] == "handed_off" and "End your reply" in result["note"]
    assert authority.snapshot()["runId"] is None
    await _until(lambda: surfaces.cards)
    wire = clarify_to_wire(surfaces.cards[0])
    assert wire["id"] == result["stepId"] and wire["kind"] == "computer" and wire["profileId"] == "default"
    assert wire["sessionKey"] == "desktop:chat" and wire["question"].startswith("Sign in to Amazon")
    store = ScreenSteps(browser.runtime.directory)
    assert store.waiting("desktop:chat")["id"] == result["stepId"]
    # The person opens the screen, takes control, signs in, hands back.
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    await _until(lambda: store.get(result["stepId"])["taken_generation"] == "generation")
    assert surfaces.turns == [] and get_clarify_manager().get_pending(result["stepId"]) is not None
    authority.release("human", "generation", human["epoch"])
    await _until(lambda: surfaces.turns)
    session_key, channel, chat_id, content = surfaces.turns[0]
    assert (session_key, channel, chat_id) == ("desktop:chat", "desktop", "chat")
    assert content.startswith("[Screen] The person did this on your screen and handed it back: Sign in to Amazon")
    assert "read_page" in content
    assert surfaces.closed == [(result["stepId"], "done")]
    assert not get_clarify_manager().list_pending()
    assert [step["state"] for step in store.recent("desktop:chat")] == ["done"]


async def test_a_screen_the_person_already_holds_continues_on_the_hand_back(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    authority = browser.runtime.authority
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    async with host_screen_run_scope(gateway, "desktop:chat", "run"):
        # The browser refuses while the person has the screen, and says what to do.
        refused = await gateway.send_browser_tool_request("open", "get_page_text", {})
        assert refused["error_code"] == "HUMAN_HAS_CONTROL" and "handoff" in refused["error"]
        result = json.loads(await tool(gateway, surfaces.continuation).execute("handoff", task="Finish signing in"))
    assert result["outcome"] == "handed_off"
    await _until(lambda: surfaces.cards)
    authority.release("human", "generation", human["epoch"])
    await _until(lambda: surfaces.turns)
    assert surfaces.turns[0][3].startswith("[Screen] The person did this on your screen and handed it back: Finish signing in")


async def test_later_is_not_a_new_step_and_the_turn_is_told_a_step_waits(screen_gateway, surfaces):
    """Hakan, 2026-10-20: "ok thanks, I'll look in a bit" produced a second card."""
    from flowly.agent.loop import screen_step_waiting_guidance

    gateway, _, browser = screen_gateway
    first = await hand_over(gateway, surfaces, "Sign in to X")
    again = await hand_over(gateway, surfaces, "Sign in to X", run="run-2")
    assert again["outcome"] == "already_waiting" and again["stepId"] == first["stepId"]
    assert "Do not hand it over again" in again["note"]
    await asyncio.sleep(0.02)
    assert len(surfaces.cards) == 1 and surfaces.closed == []
    waiting = module.waiting_step(gateway, "desktop:chat")
    assert waiting["id"] == first["stepId"]
    guidance = screen_step_waiting_guidance(waiting, now=waiting["created_at"] + 125)
    assert '"Sign in to X" (handed over 2 min ago)' in guidance and "withdraw" in guidance
    assert module.waiting_step(gateway, "desktop:other") is None


async def test_a_different_step_supersedes_the_one_waiting_and_both_are_on_record(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    first = await hand_over(gateway, surfaces, "Sign in")
    second = await hand_over(gateway, surfaces, "Enter the code", run="run-2")
    await _until(lambda: surfaces.closed)
    assert surfaces.closed == [(first["stepId"], "superseded")]
    assert [p.question for p in get_clarify_manager().list_pending()] == ["Enter the code"]
    store = ScreenSteps(browser.runtime.directory)
    assert [(s["task"], s["state"]) for s in store.recent("desktop:chat")] == [("Sign in", "superseded"), ("Enter the code", "waiting")]
    take_and_hand_back(browser.runtime.authority)
    await _until(lambda: surfaces.turns)
    assert len(surfaces.turns) == 1 and "Enter the code" in surfaces.turns[0][3]
    assert store.get(second["stepId"])["state"] == "done"


async def test_the_agent_withdraws_a_step_the_person_gave_up_on(screen_gateway, surfaces):
    """Hakan: "I won't sign in, I give up, remove the card" must not need the screen."""
    gateway, _, browser = screen_gateway
    result = await hand_over(gateway, surfaces, "Sign in to X")
    async with host_screen_run_scope(gateway, "desktop:chat", "run-tr"):
        again = await tool(gateway, surfaces.continuation).execute("handoff", task="Sign in to X")
    assert '"Sign in to X"' in again  # the step as written: Turkish below stays Turkish too
    await _until(lambda: surfaces.cards)
    async with host_screen_run_scope(gateway, "desktop:chat", "run-2"):
        withdrawn = json.loads(await tool(gateway, surfaces.continuation).execute("withdraw"))
        nothing = json.loads(await tool(gateway, surfaces.continuation).execute("withdraw"))
    assert withdrawn["outcome"] == "withdrawn" and withdrawn["stepId"] == result["stepId"]
    assert nothing["outcome"] == "nothing_waiting"
    await _until(lambda: surfaces.closed)
    assert surfaces.closed == [(result["stepId"], "withdrawn")]
    assert ScreenSteps(browser.runtime.directory).get(result["stepId"])["state"] == "withdrawn"
    assert not get_clarify_manager().list_pending()
    # A hand-back later is not this step's: no continuation.
    take_and_hand_back(browser.runtime.authority)
    await asyncio.sleep(0.03)
    assert surfaces.turns == []


async def test_a_stopped_screen_keeps_the_step_waiting_until_it_is_done_on_the_next_one(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    authority = browser.runtime.authority
    result = await hand_over(gateway, surfaces, "Sign in")
    await _until(lambda: surfaces.cards)
    # The person takes the screen, and it stops before they hand back (a crash, an explicit stop).
    authority.acquire("human", "generation", authority.snapshot()["epoch"])
    store = ScreenSteps(browser.runtime.directory)
    await _until(lambda: store.get(result["stepId"])["taken_generation"] == "generation")
    authority.stop_dead("generation")
    await asyncio.sleep(0.03)
    assert surfaces.closed == [] and surfaces.turns == []
    # Opening the computer starts the screen again; the step is done on that one.
    authority.start("generation-2")
    await _until(lambda: store.get(result["stepId"])["watch_generation"] == "generation-2")
    assert surfaces.turns == []  # agent control on a new screen is not a hand-back
    take_and_hand_back(authority, "generation-2")
    await _until(lambda: surfaces.turns)
    assert surfaces.closed == [(result["stepId"], "done")]
    assert store.get(result["stepId"])["taken_generation"] == "generation"


async def test_a_restarted_gateway_watches_the_waiting_step_again(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    result = await hand_over(gateway, surfaces, "Sign in")
    await _until(lambda: surfaces.cards)
    fresh = surfaces.restart()
    assert fresh.list_pending() == []
    # In this process the old watcher is cancelled and the old manager says so;
    # a dead process says nothing. What follows is the new process's story.
    await asyncio.sleep(0.01)
    surfaces.closed.clear()
    assert module.recover(gateway, surfaces.continuation) == 1
    await _until(lambda: len(surfaces.cards) == 2)
    assert surfaces.cards[1].id == result["stepId"]  # the card comes back on the same id
    assert module.recover(gateway, surfaces.continuation) == 1  # idempotent: one watcher per step
    take_and_hand_back(browser.runtime.authority)
    await _until(lambda: surfaces.turns)
    assert surfaces.closed == [(result["stepId"], "done")]


async def test_a_step_taken_before_the_restart_is_done_on_the_hand_back_after_it(screen_gateway, surfaces):
    gateway, _, browser = screen_gateway
    authority = browser.runtime.authority
    result = await hand_over(gateway, surfaces, "Sign in")
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    store = ScreenSteps(browser.runtime.directory)
    await _until(lambda: store.get(result["stepId"])["taken_generation"] == "generation")
    surfaces.restart()
    module.recover(gateway, surfaces.continuation)
    authority.release("human", "generation", human["epoch"])
    await _until(lambda: surfaces.turns)
    assert "handed it back: Sign in" in surfaces.turns[0][3]


async def test_nobody_within_the_horizon_tells_the_agent_once(screen_gateway, surfaces, monkeypatch):
    gateway, _, browser = screen_gateway
    monkeypatch.setattr(module, "HORIZON_SECONDS", 0.1)
    result = await hand_over(gateway, surfaces, "Solve the CAPTCHA")
    await _until(lambda: surfaces.turns)
    assert surfaces.turns[0][3].startswith("[Screen] Nobody did this on your screen: Solve the CAPTCHA")
    assert surfaces.closed == [(result["stepId"], "timeout")]
    assert ScreenSteps(browser.runtime.directory).get(result["stepId"])["state"] == "not_done"


async def test_outside_desktop_nothing_is_handed_over(screen_gateway, surfaces):
    gateway, _, _ = screen_gateway
    async with host_screen_run_scope(gateway, "telegram:42", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        result = json.loads(await tool(gateway, surfaces.continuation, "telegram", "42").execute("handoff", task="Sign in"))
        run.succeeded = True
    assert result["outcome"] == "unavailable" and "Flowly Desktop" in result["note"]
    assert surfaces.cards == [] and surfaces.turns == []


async def test_without_a_screen_there_is_no_card(surfaces):
    result = json.loads(await BrowserTabTool(None, screen_continuation=surfaces.continuation).execute("handoff", task="Sign in"))
    assert result["outcome"] == "unavailable" and surfaces.cards == []
    nothing = json.loads(await BrowserTabTool(None, screen_continuation=surfaces.continuation).execute("withdraw"))
    assert nothing["outcome"] == "nothing_waiting"


def test_the_handoff_costs_the_browser_tool_two_actions_and_one_argument():
    schema = BrowserTabTool(None).parameters
    assert {"handoff", "withdraw"} <= set(schema["properties"]["action"]["enum"])
    assert len(schema["properties"]["task"]["description"]) < 200


def test_clients_read_the_conversations_steps_from_the_record(tmp_path):
    assert module.recent_steps(tmp_path / "nowhere", "desktop:chat") == []
    assert not (tmp_path / "nowhere").exists()  # a read makes nothing
    store = ScreenSteps(tmp_path)
    step, _ = store.open({"hostId": "h", "botId": "b", "profileId": "default"}, "desktop:chat", "desktop", "chat", "Sign in")
    store.resolve(step["id"], "withdrawn")
    (seen,) = module.recent_steps(tmp_path, "desktop:chat")
    assert seen["id"] == step["id"] and seen["state"] == "withdrawn" and "chat_id" not in seen


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



async def test_the_step_reaches_the_agent_as_written_not_escaped(screen_gateway, surfaces):
    """2026-10-21: a Turkish step came back as X\\u2019in giri\\u015f ekran\\u0131nda...,
    costing the model tokens and showing escapes in the tool panel."""
    gateway, _, _ = screen_gateway
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        raw = await tool(gateway, surfaces.continuation).execute("handoff", task="X’in giriş ekranında oturum aç")
        run.succeeded = True
    assert "X’in giriş ekranında oturum aç" in raw and "\\u" not in raw
