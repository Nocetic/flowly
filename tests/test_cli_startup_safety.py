"""CLI startup must not race tool discovery or expose local variables."""

from __future__ import annotations

import io
import threading

import pytest
import typer
from rich.console import Console

from flowly.agent.loop import AgentLoop
from flowly.bus.queue import MessageBus
from flowly.cli import agent_cmd
from flowly.cli.agent_cmd import (
    _mcp_discovery_wait_timeout,
    _offer_semantic_tool_routing,
    _run_agent_coroutine,
    _run_agent_coroutine_with_cleanup,
    _wait_for_initial_mcp_tools,
)
from flowly.cli.commands import app
from flowly.config.schema import Config, MCPServerConfig
from flowly.providers.base import LLMProvider, LLMResponse


class _NoopProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args, **kwargs) -> LLMResponse:
        raise AssertionError("No model call expected")


def test_cli_pretty_exceptions_never_show_locals() -> None:
    """Config objects can contain credentials, so Rich locals stay disabled."""

    assert app.pretty_exceptions_show_locals is False


def test_mcp_wait_timeout_tracks_slowest_enabled_server() -> None:
    config = Config()
    config.mcp_servers = {
        "fast": MCPServerConfig(command="fast", connect_timeout=2),
        "slow": MCPServerConfig(command="slow", connect_timeout=17),
        "disabled": MCPServerConfig(
            command="disabled",
            enabled=False,
            connect_timeout=120,
        ),
    }

    assert _mcp_discovery_wait_timeout(config) == 22


def test_mcp_wait_timeout_is_zero_without_enabled_servers() -> None:
    config = Config()
    config.mcp_servers = {
        "disabled": MCPServerConfig(command="disabled", enabled=False),
    }

    assert _mcp_discovery_wait_timeout(config) == 0


def test_agent_loop_readiness_contract_is_bounded() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._mcp_discovery_started = True
    loop._mcp_discovery_done = threading.Event()

    assert loop.mcp_discovery_pending is True
    assert loop.wait_for_mcp_discovery(timeout=0) is False

    loop._mcp_discovery_done.set()

    assert loop.wait_for_mcp_discovery(timeout=0) is True
    assert loop.mcp_discovery_pending is False


def test_agent_loop_signals_real_background_discovery_completion(
    tmp_path,
    monkeypatch,
) -> None:
    import flowly.mcp

    entered = threading.Event()
    release = threading.Event()

    def fake_discovery(*, servers, tool_registry):
        entered.set()
        assert release.wait(timeout=2)
        return ["mcp_docs_query"]

    monkeypatch.setattr(flowly.mcp, "discover_mcp_tools", fake_discovery)
    config = Config()
    config.mcp_servers = {
        "docs": MCPServerConfig(command="docs", connect_timeout=1),
    }

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_NoopProvider(),
        workspace=tmp_path,
        main_config=config,
    )

    assert entered.wait(timeout=2)
    assert loop.mcp_discovery_pending is True
    release.set()
    assert loop.wait_for_mcp_discovery(timeout=2) is True
    assert loop._mcp_discovery_registered == 1


def test_cli_waits_for_pending_catalog_before_first_turn(monkeypatch) -> None:
    config = Config()
    config.mcp_servers = {
        "docs": MCPServerConfig(command="docs", connect_timeout=3),
    }

    class FakeLoop:
        mcp_discovery_pending = True

        def __init__(self) -> None:
            self.waited: list[float] = []

        def wait_for_mcp_discovery(self, timeout: float) -> bool:
            self.waited.append(timeout)
            return True

    fake = FakeLoop()
    monkeypatch.setattr(
        agent_cmd,
        "console",
        Console(file=io.StringIO(), force_terminal=False),
    )

    assert _wait_for_initial_mcp_tools(fake, config) is True
    assert fake.waited == [8]


def test_interactive_semantic_offer_persists_decline_once(monkeypatch) -> None:
    from flowly.agent.tools import semantic_feature

    output = io.StringIO()
    monkeypatch.setattr(
        agent_cmd,
        "console",
        Console(file=output, force_terminal=False),
    )
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *args, **kwargs: False)
    monkeypatch.setattr(semantic_feature, "feature_status", lambda metrics: {
        "state": "recommended",
        "deferredToolCount": 20,
        "estimatedTokensSavedPerTypicalTurn": 8_000,
        "modelBytes": 257_205_577,
    })
    persisted = []
    monkeypatch.setattr(
        semantic_feature,
        "persist_semantic_preference",
        lambda **kwargs: persisted.append(kwargs),
    )

    class FakeLoop:
        @staticmethod
        def semantic_tool_routing_metrics():
            return {"deferredToolCount": 20, "deferredSchemaTokens": 6_000}

    _offer_semantic_tool_routing(FakeLoop())

    assert persisted == [{
        "consent": "dismissed",
        "enabled": False,
        "dismissed_tool_count": 20,
        "dismissed_schema_tokens": 6_000,
    }]


def test_agent_cli_redacts_unexpected_runtime_errors(monkeypatch) -> None:
    output = io.StringIO()
    monkeypatch.setattr(
        agent_cmd,
        "console",
        Console(file=output, force_terminal=False),
    )

    async def fail() -> None:
        raise RuntimeError("request failed with Bearer topsecret123")

    with pytest.raises(typer.Exit) as caught:
        _run_agent_coroutine(fail())

    assert caught.value.exit_code == 1
    rendered = output.getvalue()
    assert "topsecret123" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize("should_fail", [False, True])
def test_agent_cli_always_stops_background_resources(
    monkeypatch,
    should_fail: bool,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(
        agent_cmd,
        "console",
        Console(file=output, force_terminal=False),
    )

    class FakeLoop:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1

    async def operation() -> str:
        if should_fail:
            raise RuntimeError("expected failure")
        return "ok"

    loop = FakeLoop()
    if should_fail:
        with pytest.raises(typer.Exit):
            _run_agent_coroutine_with_cleanup(operation(), loop)
    else:
        assert _run_agent_coroutine_with_cleanup(operation(), loop) == "ok"

    assert loop.stop_calls == 1
