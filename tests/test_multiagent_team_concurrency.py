"""Team routing and CLI process concurrency stay bounded."""

import asyncio

import pytest

from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tools.delegate import DelegateTool
from flowly.bus.queue import MessageBus
from flowly.config.loader import convert_keys
from flowly.config.schema import AgentsConfig, MultiAgentConfig, MultiAgentTeamConfig
from flowly.multiagent.orchestrator import TeamOrchestrator
from flowly.multiagent.router import AgentRouter, TeamContext


def team_setup(size: int = 8):
    agents = {"leader": MultiAgentConfig(name="Leader")}
    agents.update({f"member{i}": MultiAgentConfig() for i in range(size)})
    team = MultiAgentTeamConfig(
        name="Review", agents=list(agents), leader_agent="leader"
    )
    router = AgentRouter(agents, {"review": team})
    return agents, team, router


async def test_team_fanout_uses_bounded_workers_and_preserves_order(tmp_path, monkeypatch):
    agents, team, router = team_setup(size=64)
    first_wave = asyncio.Event()
    release = asyncio.Event()
    started = active = peak = 0

    async def fake_invoke(_agent, agent_id, _message, _workspace):
        nonlocal started, active, peak
        if agent_id == "leader":
            return " ".join(f"[@member{i}: task {i}]" for i in range(64))
        started += 1
        active += 1
        peak = max(peak, active)
        if started == 3:
            first_wave.set()
        await release.wait()
        active -= 1
        return f"result {agent_id}"

    monkeypatch.setattr("flowly.multiagent.orchestrator.invoke_agent", fake_invoke)
    orchestrator = TeamOrchestrator(router, max_concurrent=3)
    task = asyncio.create_task(orchestrator.execute(
        "Review it", "leader", TeamContext("review", team), agents, tmp_path
    ))
    try:
        await asyncio.wait_for(first_wave.wait(), 2)
        await asyncio.sleep(0)
        assert started == peak == active == 3
    finally:
        release.set()
    result = await task
    assert started == 64
    assert peak == 3
    assert [step.agent_id for step in result.steps] == [
        "leader", *(f"member{i}" for i in range(64))
    ]


async def test_team_route_delivers_all_steps_in_background(tmp_path, monkeypatch):
    agents, team, router = team_setup(size=2)
    route = router.route("@review Check this")
    assert (route.agent_id, route.team_id, route.is_team) == (
        "leader", "review", True
    )

    async def fake_invoke(_agent, agent_id, _message, _workspace):
        if agent_id == "leader":
            return "[@member0: first] [@member1: second]"
        return f"Done by {agent_id}"

    monkeypatch.setattr("flowly.multiagent.orchestrator.invoke_agent", fake_invoke)
    bus = MessageBus()
    registry = SubagentRegistry(tmp_path / "runs.json")
    delegate = DelegateTool(
        agents, {"review": team}, tmp_path, bus, registry=registry,
        max_concurrent=2,
    )
    delegate.set_context("web", "chat")
    acknowledgment = await delegate.execute(
        route.agent_id, route.message, team_id=route.team_id
    )
    assert "@review" in acknowledgment
    await asyncio.gather(*list(delegate._tasks.values()))
    inbound = await bus.consume_inbound()
    assert inbound.content.startswith("[DELEGATE_RESULT:review]")
    record = registry.all()[0]
    assert record.outcome == "ok"
    assert record.agent_id == "review"
    assert "@member0: Done by member0" in registry.read_result(record.run_id)["content"]
    assert "@member1: Done by member1" in registry.read_result(record.run_id)["content"]
    await delegate._events.flush()


async def test_direct_and_team_calls_share_process_limit(tmp_path, monkeypatch):
    agents, team, _ = team_setup(size=1)
    agents["direct"] = MultiAgentConfig()
    direct_started = asyncio.Event()
    release_direct = asyncio.Event()
    calls = []

    async def fake_direct(_agent, agent_id, _message, _workspace, **_kwargs):
        calls.append(agent_id)
        direct_started.set()
        await release_direct.wait()
        return "Direct done"

    async def fake_team(_agent, agent_id, _message, _workspace):
        calls.append(agent_id)
        return "Team done"

    monkeypatch.setattr("flowly.agent.tools.delegate.invoke_agent", fake_direct)
    monkeypatch.setattr("flowly.multiagent.orchestrator.invoke_agent", fake_team)
    delegate = DelegateTool(
        agents, {"review": team}, tmp_path, MessageBus(),
        registry=SubagentRegistry(tmp_path / "runs.json"), max_concurrent=1,
    )
    await delegate.execute("direct", "First")
    await asyncio.wait_for(direct_started.wait(), 2)
    await delegate.execute("leader", "Second", team_id="review")
    try:
        await asyncio.sleep(0)
        assert calls == ["direct"]
    finally:
        release_direct.set()
    await asyncio.gather(*list(delegate._tasks.values()))
    assert calls == ["direct", "leader"]
    await delegate._events.flush()


def test_cli_agent_limit_is_validated():
    assert AgentsConfig().max_concurrent_cli_agents == 5
    assert AgentsConfig.model_validate(
        convert_keys({"maxConcurrentCliAgents": 3})
    ).max_concurrent_cli_agents == 3
    with pytest.raises(ValueError):
        AgentsConfig(max_concurrent_cli_agents=0)
    with pytest.raises(ValueError):
        AgentsConfig(max_concurrent_cli_agents=65)
