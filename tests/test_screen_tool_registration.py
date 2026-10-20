"""A default CLI agent can use its installed Screen on the first model turn."""

import copy
import json

import pytest

from flowly.agent.loop import SCREEN_HANDOFF_GUIDANCE, AgentLoop
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.desktop_runtime.host_screen_run import host_screen_run_scope
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from tests.test_screen_gateway_binding import screen_gateway as gateway_fixture

screen_gateway = gateway_fixture


@pytest.fixture(autouse=True)
def linux(monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "linux")


class FirstTurnBrowser(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = []

    def get_default_model(self):
        return "test/model"

    async def chat(self, *args, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            assert "browser_tab" in {d["function"]["name"] for d in kwargs["tools"]}
            assert any(m.get("content") == SCREEN_HANDOFF_GUIDANCE for m in kwargs["messages"])
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="first-navigation", name="browser_tab",
                arguments={"action": "navigate", "url": "https://example.com"})])
        return LLMResponse(content="Opened")


@pytest.fixture
def agent(screen_gateway, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "agent-home"))
    model = FirstTurnBrowser()
    loop = AgentLoop(bus=MessageBus(), provider=model, workspace=tmp_path / "workspace",
                     main_config=Config(), max_iterations=3, soft_warn_at_iteration=0)
    loop.set_gateway_server(screen_gateway[0])
    yield loop, model
    loop.stop()


async def test_default_cli_first_turn_navigates_through_screen_tool(agent, screen_gateway):
    loop, model = agent
    gateway, _, browser = screen_gateway
    assert loop._main_config.tools.browser_tab.enabled is False
    assert loop._main_config.tools.computer.enabled is False
    assert loop.tools.get("computer") is None
    assert "browser_tab" not in loop._routed_tool_names("web")
    async with host_screen_run_scope(gateway, "desktop:chat", "first-run") as run:
        assert "browser_tab" in loop._routed_tool_names("web")
        result = await loop._run_llm_tool_loop(
            [{"role": "user", "content": "x.com'u açar mısın"}],
            turn_content="x.com'u açar mısın", session_key="desktop:chat", outbound_run_id="first-run",
            tool_platform="web")
        assert result[0] == "Opened"
        assert result[2] == ["browser_tab"]
        assert run.binding is not None
        run.succeeded = True
    assert len(model.calls) == 2
    browser._dispatch.assert_awaited_once_with("navigate", {"url": "https://example.com"})
    assert "browser_tab" not in loop._routed_tool_names("web")
    # Registry caches cannot leak the schema or native access into another run.
    assert "browser_tab" not in {d["function"]["name"] for d in loop._routed_tool_definitions("web")}
    denied = json.loads(await loop.tools.get("browser_tab").execute("tabs_list"))
    assert denied["error_code"] == "SCREEN_NOT_AVAILABLE"
    browser._dispatch.assert_awaited_once()


async def test_screen_tool_respects_caller_and_toolset_blocks(agent, screen_gateway):
    loop, _ = agent
    async with host_screen_run_scope(screen_gateway[0], "chat", "blocked"):
        assert "browser_tab" not in loop._routed_tool_names("web", disabled_tools=["browser_tab"])
        assert not any(d["function"]["name"] == "browser_tab" for d in
                       loop._routed_tool_definitions("web", disabled_tools=["browser_tab"]))
        loop._main_config.tools.routing.disabled_toolsets = ["media"]
        assert "browser_tab" not in loop._routed_tool_names("web")


async def test_scope_cannot_redirect_a_tool_to_another_gateway(agent, screen_gateway):
    loop, _ = agent
    from types import SimpleNamespace
    other = SimpleNamespace(_get_host_screen_browser=screen_gateway[0]._get_host_screen_browser)
    async with host_screen_run_scope(other, "chat", "other"):
        assert "browser_tab" not in loop._routed_tool_names("web")
        result = json.loads(await loop.tools.get("browser_tab").execute("tabs_list"))
        assert result["error_code"] == "SCREEN_NOT_AVAILABLE"


@pytest.mark.parametrize("platform,screen_enabled,native_enabled,expected", [
    ("linux", True, False, True), ("linux", False, False, False),
    ("darwin", True, False, False), ("win32", True, False, False),
    ("darwin", True, True, True), ("linux", False, True, True),
])
def test_registration_keeps_native_opt_in(tmp_path, monkeypatch, platform, screen_enabled, native_enabled, expected):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "agent-home"))
    # Mock only registration's platform, not Python's global platform-dependent imports.
    from types import SimpleNamespace

    import flowly.agent.loop as module
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    config = Config()
    config.tools.browser_tab.enabled = native_enabled
    config.tools.browser_tab.screen_enabled = screen_enabled
    loop = AgentLoop(bus=MessageBus(), provider=FirstTurnBrowser(), workspace=tmp_path / "workspace", main_config=config)
    try:
        browser = loop.tools.get("browser_tab")
        assert (browser is not None) == expected
        if browser:
            assert browser.screen_only == (not native_enabled)
    finally:
        loop.stop()


async def test_takeover_is_respected_before_first_navigation(agent, screen_gateway):
    loop, _ = agent
    gateway, _, browser = screen_gateway
    authority = browser.runtime.authority
    human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
    async with host_screen_run_scope(gateway, "chat", "held"):
        result = json.loads(await loop.tools.get("browser_tab").execute("navigate", url="https://example.com"))
        assert result["error_code"] == "HUMAN_HAS_CONTROL"
    assert authority.snapshot() == human
    browser._dispatch.assert_not_awaited()


async def test_screen_disappearing_before_claim_never_falls_back(agent, screen_gateway, monkeypatch):
    from unittest.mock import AsyncMock

    loop, _ = agent
    gateway, _, browser = screen_gateway
    async with host_screen_run_scope(gateway, "chat", "gone") as run:
        monkeypatch.setattr(run, "bind", AsyncMock(return_value=None))
        result = json.loads(await loop.tools.get("browser_tab").execute("tabs_list"))
        assert result["error_code"] == "SCREEN_NOT_AVAILABLE"
    browser._dispatch.assert_not_awaited()


async def test_explicit_screen_opt_out_does_not_fall_back_to_native(agent, screen_gateway):
    loop, _ = agent
    tool = loop.tools.get("browser_tab")
    tool.screen_only = False  # native browser opt-in
    tool.screen_enabled = False
    async with host_screen_run_scope(screen_gateway[0], "chat", "disabled"):
        assert "browser_tab" not in loop._routed_tool_names("web")
        result = json.loads(await tool.execute("tabs_list"))
        assert result["error_code"] == "SCREEN_DISABLED"
    screen_gateway[2]._dispatch.assert_not_awaited()
