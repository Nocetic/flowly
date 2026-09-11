import asyncio

import pytest

from flowly.agent.run_steering import ProviderSteeredError, RunSteeringController, SteeringError


def request(**overrides):
    return dict(
        runId="run",
        sessionKey="chat",
        idempotencyKey="guidance",
        message="Make it shorter",
        **overrides,
    )


def test_acceptance_is_durable_idempotent_and_scoped():
    controller = RunSteeringController()
    saved = []
    with controller.register(
        "run", "chat", lambda message, receipt: saved.append((message, receipt))
    ):
        first = controller.submit(request(), lambda: {"content": "Make it shorter"})
        assert first["accepted"] and "digest" not in first
        assert controller.submit(request(), lambda: pytest.fail("prepared twice")) == first
        assert len(saved) == 1
        assert len(controller.take("run")) == 1
        bad = request()
        bad["sessionKey"] = "other"
        with pytest.raises(SteeringError, match="ended"):
            controller.submit(bad, lambda: {})
        bad = request()
        bad["message"] = "different"
        with pytest.raises(SteeringError, match="identity"):
            controller.submit(bad, lambda: {})
        controller.close("run")
        assert controller.submit(request(), lambda: {}) == first
        bad = request()
        bad["idempotencyKey"] = "late"
        with pytest.raises(SteeringError, match="no longer"):
            controller.submit(bad, lambda: {})


def test_failed_persistence_does_not_accept_or_interrupt():
    controller = RunSteeringController()

    def fail(*_):
        raise OSError("disk full")

    with controller.register("run", "chat", fail):
        with pytest.raises(OSError):
            controller.submit(request(), lambda: {"content": "x"})
        assert not controller.has_pending("run")
        assert not controller.runs["run"].receipts


@pytest.mark.asyncio
async def test_silent_provider_is_interrupted_but_parent_and_other_runs_are_not():
    controller = RunSteeringController()
    entered = asyncio.Event()

    async def silent():
        entered.set()
        await asyncio.Event().wait()

    with controller.register("run", "chat", lambda *_: None):
        provider = asyncio.create_task(controller.provider_call("run", silent))
        other = asyncio.create_task(asyncio.sleep(0.1, result="tool finished"))
        await entered.wait()
        controller.submit(request(), lambda: {"content": "shorter"})
        with pytest.raises(ProviderSteeredError):
            await asyncio.wait_for(provider, 1)
        assert await other == "tool finished"
        assert controller.take("run")[0]["id"] == "guidance"
        assert (
            await controller.provider_call("run", lambda: asyncio.sleep(0, result="continued"))
            == "continued"
        )


@pytest.mark.asyncio
async def test_stop_cancellation_wins_over_simultaneous_steer():
    controller = RunSteeringController()
    entered = asyncio.Event()

    async def silent():
        entered.set()
        await asyncio.Event().wait()

    with controller.register("run", "chat", lambda *_: None):
        task = asyncio.create_task(controller.provider_call("run", silent))
        await entered.wait()
        controller.submit(request(), lambda: {"content": "x"})
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("use_tools", [False, True])
async def test_real_turn_continues_after_guidance_and_receipt_survives_reload(
    tmp_path, monkeypatch, use_tools
):
    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.base import Tool
    from flowly.bus.queue import MessageBus
    from flowly.channels import feature_rpc
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
    from flowly.session.manager import SessionManager

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    waiting = asyncio.Event()
    chunks = []
    events = []

    class Lookup(Tool):
        name = "steer_lookup"
        description = "Read fixture information."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "fixture result"

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, **kwargs):
            return LLMResponse(content="A short story.")

        async def chat_stream(self, **kwargs):
            self.calls.append(kwargs["messages"].copy())
            if len(self.calls) == 1:
                yield LLMResponse(content="Once upon a time")
                waiting.set()
                await asyncio.Event().wait()
            elif use_tools and len(self.calls) == 2:
                yield LLMResponse(
                    content="Looking up.",
                    finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="lookup", name="steer_lookup", arguments={})],
                )
            else:
                yield LLMResponse(content="A short ending.", finish_reason="stop")

    provider = Provider()
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )

    agent.tools.register(Lookup())

    async def on_iteration(event):
        events.append(event)

    async def chunk(text):
        chunks.append(text)

    task = asyncio.create_task(
        agent.process_direct(
            "Tell me a story.",
            session_key="web:story",
            run_id="story",
            stream_callback=chunk,
            skip_memory=True,
            skip_context_files=True,
            tools_allowed=use_tools,
            on_iteration=on_iteration,
            return_metadata=True,
        )
    )
    await asyncio.wait_for(waiting.wait(), 5)
    params = dict(
        runId="story", sessionKey="web:story", idempotencyKey="shorter", message="Make it shorter"
    )
    monkeypatch.setattr(feature_rpc, "_chat_steering_callback", agent.steer_chat)
    receipt, _ = await feature_rpc.dispatch("chat.steer", params)
    assert receipt["accepted"] is True
    result, metadata = await asyncio.wait_for(task, 5)
    assert len(provider.calls) == (3 if use_tools else 2)
    assert provider.calls[1][-1] == {"role": "user", "content": "Make it shorter"}
    if use_tools:
        assert result == "A short ending."
        assert events[0]["content"] == "Once upon a time\n\nLooking up."
    else:
        assert result == "".join(chunks) == "Once upon a time\n\nA short ending."
    assert metadata.get("aborted") is not True
    assert not agent.is_run_aborted("story")
    assert not agent._steering().runs
    # Canonical save must contain guidance exactly once, before the same run's answer.
    history = agent.sessions.get_full_messages("web:story")
    assert [row["role"] for row in history] == (
        ["user", "user", "assistant", "tool", "assistant"]
        if use_tools
        else ["user", "user", "assistant"]
    )
    assert sum("Once upon a time" in str(row["content"]) for row in history) == 1
    assert history[1]["id"] == "shorter"
    assert history[-1]["run_id"] == "story"
    agent.sessions = SessionManager(tmp_path)
    assert agent.steer_chat(params) == receipt
    assert len(provider.calls) == (3 if use_tools else 2)
    late = {**params, "idempotencyKey": "too-late"}
    with pytest.raises(SteeringError):
        agent.steer_chat(late)


@pytest.mark.asyncio
async def test_guidance_during_tool_waits_for_result_then_enters_same_run(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.base import Tool
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    class SlowTool(Tool):
        name = "steer_test_read"
        description = "Read a fixture."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            entered.set()
            await release.wait()
            completed.append("read finished")
            return "fixture result"

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, **kwargs):
            self.calls.append(kwargs["messages"].copy())
            if len(self.calls) == 1:
                return LLMResponse(
                    content="Reading.",
                    tool_calls=[ToolCallRequest(id="read", name="steer_test_read", arguments={})],
                )
            return LLMResponse(content="Concise result.")

    provider = Provider()
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    agent.tools.register(SlowTool())
    journal = []
    with agent._steering().register("run", "chat", lambda *_: None):
        task = asyncio.create_task(
            agent._run_llm_tool_loop(
                messages=[
                    {"role": "system", "content": "test"},
                    {"role": "user", "content": "Read the fixture."},
                ],
                action_turn=False,
                session_key="chat",
                outbound_run_id="run",
                tool_platform="web",
                turn_messages_out=journal,
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        agent._steering().submit(request(), lambda: {"content": "Make it shorter"})
        await asyncio.sleep(0)
        assert not task.done() and completed == []
        release.set()
        result, *_ = await asyncio.wait_for(task, 5)
    assert result == "Concise result."
    assert completed == ["read finished"]
    last = provider.calls[-1]
    guidance_index = next(i for i, m in enumerate(last) if m.get("content") == "Make it shorter")
    result_index = next(i for i, m in enumerate(last) if m.get("role") == "tool")
    assert result_index < guidance_index
    assert len([m for m in journal if m.get("id") == "guidance"]) == 1


@pytest.mark.asyncio
async def test_rpc_distinguishes_a_finished_run_from_unconfirmed_delivery(monkeypatch):
    from flowly.channels import feature_rpc

    def finished(_):
        raise SteeringError("RUN_NOT_ACTIVE", "Response ended")

    monkeypatch.setattr(feature_rpc, "_chat_steering_callback", finished)
    result, _ = await feature_rpc.dispatch("chat.steer", request())
    assert result == {
        "accepted": False,
        "code": "RUN_NOT_ACTIVE",
        "runId": "run",
        "sessionKey": "chat",
        "messageId": "guidance",
    }
