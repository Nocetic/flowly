"""Production gateway/agent-loop continuation with a deterministic local model."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.host_screen_run import host_screen_run_scope
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from tests.test_screen_gateway_binding import screen_gateway as gateway_fixture

screen_gateway = gateway_fixture


class LocalModel(LLMProvider):
    def __init__(self, authority):
        super().__init__(api_key="test")
        self.authority = authority
        self.calls = []

    def get_default_model(self):
        return "test/model"

    async def chat(self, *args, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            self.authority.acquire("human", "generation", self.authority.snapshot()["epoch"])
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="stale-click", name="browser_tab",
                arguments={"action": "click", "ref": "old-ref"})], usage={"completion_tokens": 10})
        return LLMResponse(content="Continued the original task", usage={"completion_tokens": 20})


async def wait_state(store, state):
    async with asyncio.timeout(5):
        while (await asyncio.to_thread(store.status) or {}).get("state") != state:
            await asyncio.sleep(0.005)


@pytest.fixture
async def live_run(screen_gateway, tmp_path, monkeypatch):
    gateway, socket, browser = screen_gateway
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "agent-home"))
    monkeypatch.setattr("flowly.desktop_runtime.run_checkpoint.ScreenRunCheckpoint.INTERVAL", 0.005)
    browser._dispatch.return_value = {"success": True, "elements": "ref_fresh button Continue"}
    authority = browser.runtime.authority
    model = LocalModel(authority)
    loop = AgentLoop(bus=MessageBus(), provider=model, workspace=tmp_path / "workspace", main_config=Config(),
                     max_iterations=4, soft_warn_at_iteration=0)
    loop.tools.execute = AsyncMock(side_effect=AssertionError("stale model tool must never execute"))
    result = {}

    async def chat(*args):
        # Production enters this scope in AgentLoop._process_message.
        async with host_screen_run_scope(gateway, args[0], args[2]) as run:
            # The task's first browser action claims the running screen.
            assert (await gateway.send_browser_tool_request("open", "get_page_text", {}))["success"]
            result["loop"] = await loop._run_llm_tool_loop(
                [{"role": "system", "content": "Complete the browser task."}, {"role": "user", "content": "Continue."}],
                action_turn=False, session_key=args[0], outbound_run_id=args[2], tools_allowed=False)
            run.succeeded = not loop.is_run_aborted(args[2])
        return result["loop"][0], {"aborted": loop.is_run_aborted(args[2])}

    gateway.on_chat_message = chat
    task = asyncio.create_task(gateway._run_chat(socket, "viewer", "original-chat", "work", "origin-run", AsyncMock()))
    store = RunHandoffs(browser.runtime.directory, authority)
    try:
        await wait_state(store, "waiting_human")
        yield gateway, socket, browser, loop, model, store, task, result
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        loop.stop()


async def test_same_agent_loop_waits_for_explicit_resume_and_discards_old_model_actions(live_run):
    _, _, browser, loop, model, store, task, result = live_run
    authority = browser.runtime.authority
    assert len(model.calls) == 1
    held = authority.snapshot()
    authority.release("human", "generation", held["epoch"])
    await wait_state(store, "waiting_resume")
    await asyncio.sleep(0.03)
    assert len(model.calls) == 1 and not task.done()
    status = store.status()
    assert status["originSessionKey"] == "original-chat"
    assert status["originRunId"] == "origin-run"
    store.request_resume(status["handoffId"], status["controlEpoch"], "explicit-resume")
    await asyncio.wait_for(task, 5)
    assert len(model.calls) == 2
    assert "ref_fresh" in str(model.calls[1]["messages"])
    assert "untrusted page content" in str(model.calls[1]["messages"])
    assert result["loop"][3]["completion_tokens"] == 30
    loop.tools.execute.assert_not_awaited()
    assert [call.args for call in browser._dispatch.await_args_list] == [("get_page_text", {}), ("read_page", {})]
    assert store.status()["state"] == "finished"
    assert authority.snapshot()["runId"] is None


async def test_stop_interrupts_the_wait_without_releasing_human_control(live_run):
    _, _, browser, loop, model, store, task, _ = live_run
    loop.mark_aborted("origin-run")
    await asyncio.wait_for(task, 5)
    assert len(model.calls) == 1
    browser._dispatch.assert_awaited_once_with("get_page_text", {})
    assert store.status()["state"] == "cancelled"
    assert browser.runtime.authority.snapshot()["control"] == "human"
    assert browser.runtime.authority.snapshot()["runId"] is None


async def test_another_chat_cannot_release_the_original_run_on_a_refused_claim(live_run):
    gateway, _, browser, loop, model, store, task, _ = live_run
    async with host_screen_run_scope(gateway, "another-chat", "another-run"):
        refused = await gateway.send_browser_tool_request("other", "get_page_text", {})
    assert refused["error_code"] == "HUMAN_HAS_CONTROL"
    assert len(model.calls) == 1
    assert browser.runtime.authority.snapshot()["runId"] == "origin-run"
    assert store.status()["originSessionKey"] == "original-chat"
    assert not task.done()
    loop.mark_aborted("origin-run")
    await asyncio.wait_for(task, 5)


async def test_a_screen_run_is_told_to_hand_logins_and_codes_to_the_person(live_run):
    from flowly.agent.loop import SCREEN_HANDOFF_GUIDANCE

    _, _, _, _, model, _, _, _ = live_run
    first = model.calls[0]["messages"]
    assert any(message.get("content") == SCREEN_HANDOFF_GUIDANCE for message in first)
    for word in ("two-factor", "take control on the Screen", "do not ask them to paste passwords"):
        assert word in SCREEN_HANDOFF_GUIDANCE


async def test_a_run_without_the_screen_gets_no_screen_guidance(tmp_path, monkeypatch):
    from flowly.agent.loop import SCREEN_HANDOFF_GUIDANCE

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "agent-home"))

    class Plain(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, *args, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            return LLMResponse(content="done", usage={"completion_tokens": 1})

    model = Plain()
    loop = AgentLoop(bus=MessageBus(), provider=model, workspace=tmp_path / "workspace", main_config=Config(),
                     max_iterations=2, soft_warn_at_iteration=0)
    try:
        await loop._run_llm_tool_loop([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                                      action_turn=False, session_key="plain", outbound_run_id="plain-run",
                                      tools_allowed=False)
    finally:
        loop.stop()
    assert SCREEN_HANDOFF_GUIDANCE not in str(model.calls[0]["messages"])
