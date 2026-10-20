"""The computer card: a step the person does on the agent's screen.

Grok-style (2026-10-20): instead of saying "take control" in text, the agent
calls browser_tab's handoff action; the person gets a card that opens the screen, does the
step and hands back, and the same run continues. Real authority and handoff
record; the browser is a fake.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.agent.tools.computer_handoff import computer_handoff
from flowly.clarify.manager import get_clarify_manager
from flowly.clarify.wire import clarify_to_wire
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.host_screen_run import host_screen_run_scope
from tests.test_screen_gateway_binding import screen_gateway as gateway_fixture


@pytest.fixture(autouse=True)
def _profile_screens_run_on_linux(monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "linux")


screen_gateway = gateway_fixture


@pytest.fixture
def requests(monkeypatch):
    """What surfaces are told: the requests the manager announces."""
    manager = get_clarify_manager()
    seen: list = []

    async def notify(pending):
        seen.append(pending)

    monkeypatch.setattr(manager, "_notify_callbacks", [notify])
    monkeypatch.setattr(manager, "_close_callbacks", [])
    monkeypatch.setattr("flowly.desktop_runtime.run_checkpoint.ScreenRunCheckpoint.INTERVAL", 0.005)
    monkeypatch.setattr("flowly.agent.tools.computer_handoff._POLL_SECONDS", 0.005)
    return manager, seen


async def _first(seen):
    async with asyncio.timeout(5):
        while not seen:
            await asyncio.sleep(0.005)
    return seen[0]


async def _state(store, state):
    async with asyncio.timeout(5):
        while (await asyncio.to_thread(store.status) or {}).get("state") != state:
            await asyncio.sleep(0.005)


async def test_the_card_is_done_when_the_person_hands_the_screen_back(screen_gateway, requests):
    gateway, _, browser = screen_gateway
    manager, seen = requests
    authority = browser.runtime.authority
    store = RunHandoffs(browser.runtime.directory, authority)
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        # The agent asks through its browser tool: one action, no tool of its own.
        from flowly.agent.tools.browser_tab import BrowserTabTool

        call = asyncio.create_task(BrowserTabTool(gateway).execute(
            "handoff", task="Sign in to Amazon (and complete 2FA if asked), then hand back"))
        pending = await _first(seen)
        wire = clarify_to_wire(pending)
        assert wire["kind"] == "computer" and wire["profileId"] == "default"
        assert wire["question"].startswith("Sign in to Amazon") and wire["sessionKey"] == "desktop:chat"
        # The person opens the screen, takes control, signs in, hands back.
        human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
        await _state(store, "waiting_human")
        await asyncio.sleep(0.03)
        assert not call.done()
        authority.release("human", "generation", human["epoch"])
        await _state(store, "waiting_resume")
        status = store.status()
        store.request_resume(status["handoffId"], status["controlEpoch"], "continue")
        result = json.loads(await asyncio.wait_for(call, 5))
        assert result["outcome"] == "done"
        # The run looked at the page again and holds the screen, ready to go on.
        assert ("read_page", {}) in [c.args for c in browser._dispatch.await_args_list]
        assert store.status()["state"] == "ready"
        assert (await gateway.send_browser_tool_request("after", "get_page_text", {}))["success"]
        run.succeeded = True
    assert not manager.list_pending()


async def test_the_person_can_answer_instead_and_the_run_goes_on(screen_gateway, requests):
    gateway, _, browser = screen_gateway
    manager, seen = requests
    store = RunHandoffs(browser.runtime.directory, browser.runtime.authority)
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        call = asyncio.create_task(computer_handoff("Enter the code"))
        pending = await _first(seen)
        assert manager.resolve(pending.id, "Not now")
        result = json.loads(await asyncio.wait_for(call, 5))
        assert result["outcome"] == "answered" and result["user_response"] == "Not now"
        # Stopping the watch never marked the run cancelled.
        assert store.status()["state"] == "ready"
        assert (await gateway.send_browser_tool_request("after", "get_page_text", {}))["success"]
        run.succeeded = True


async def test_nobody_in_time_is_not_done(screen_gateway, requests, monkeypatch):
    gateway, _, _ = screen_gateway
    _, seen = requests
    monkeypatch.setattr("flowly.agent.tools.computer_handoff.TIMEOUT_SECONDS", 0.1)
    async with host_screen_run_scope(gateway, "desktop:chat", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        result = json.loads(await asyncio.wait_for(
            computer_handoff("Solve the CAPTCHA"), 5))
        run.succeeded = True
    assert result["outcome"] == "not_done" and len(seen) == 1


async def test_outside_desktop_nothing_waits(screen_gateway, requests):
    gateway, _, _ = screen_gateway
    _, seen = requests
    async with host_screen_run_scope(gateway, "telegram:42", "run") as run:
        assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
        result = json.loads(await computer_handoff("Sign in"))
        run.succeeded = True
    assert result["outcome"] == "unavailable" and "Flowly Desktop" in result["note"]
    assert seen == []


async def test_without_a_screen_there_is_no_card(requests):
    _, seen = requests
    result = json.loads(await computer_handoff("Sign in"))
    assert result["outcome"] == "unavailable" and seen == []


def test_the_handoff_costs_the_browser_tool_one_action_and_one_argument():
    from flowly.agent.tools.browser_tab import BrowserTabTool

    schema = BrowserTabTool(None).parameters
    assert "handoff" in schema["properties"]["action"]["enum"]
    assert len(schema["properties"]["task"]["description"]) < 100
