import asyncio

import pytest

from flowly.agent.run_steering import (
    STEERING_INTERRUPTED_RESULT,
    STEERING_SKIPPED_RESULT,
    ProviderSteeredError,
    RunSteeringController,
    SteeringError,
    steered_message_text,
)


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
@pytest.mark.parametrize("stop_after_steer", [False, True])
async def test_real_turn_continues_after_guidance_and_receipt_survives_reload(
    tmp_path, monkeypatch, use_tools, stop_after_steer
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
    assert receipt["checkpoint"] is None
    history_of = lambda: agent.sessions.get_full_messages("web:story")  # noqa: E731
    if stop_after_steer:
        agent.mark_aborted("story")
    result, metadata = await asyncio.wait_for(task, 5)
    history = history_of()
    # The half-written words are gone everywhere: no client shows half a sentence.
    assert not any("Once upon a time" in str(row["content"]) for row in history)
    assert history[1]["id"] == "shorter" and history[1]["turn_steer"] is True
    # Both user rows keep the time their message came, written the same way.
    assert "+" not in history[0]["timestamp"] and "+" not in history[1]["timestamp"]
    assert history[0]["timestamp"] <= history[1]["timestamp"]
    if stop_after_steer:
        assert metadata["aborted"] is True
        assert len(provider.calls) == 1
        assert [row["role"] for row in history] == ["user", "user", "assistant"]
        assert history[-1]["content"] == ""
        assert not agent._steering().runs
        assert agent.steer_chat(params) == receipt
        return
    assert len(provider.calls) == (3 if use_tools else 2)
    # The model is told the message came while it worked; the half text is not in its context.
    assert provider.calls[1][-1] == {"role": "user", "content": steered_message_text("Make it shorter")}
    assert not any("Once upon a time" in str(m.get("content")) for m in provider.calls[1])
    # The moment the agent reads it, every client learns the message joined this run.
    assert events[0] == {"runId": "story", "iterationIdx": 0, "role": "user", "content": "Make it shorter",
                         "turnSteer": True, "messageId": "shorter"}
    if use_tools:
        assert events[1]["content"] == "Looking up."
        assert events[1]["iterationIdx"] == 1
    assert result == "A short ending."
    assert metadata.get("aborted") is not True
    assert not agent.is_run_aborted("story")
    assert not agent._steering().runs
    # Canonical save: the message once, before the same run's answer; history keeps its own words.
    assert [row["role"] for row in history] == (
        ["user", "user", "assistant", "tool", "assistant"] if use_tools else ["user", "user", "assistant"]
    )
    assert history[1]["content"] == "Make it shorter"
    assert history[-1]["run_id"] == "story"
    agent.sessions = SessionManager(tmp_path)
    assert agent.steer_chat(params) == receipt
    assert len(provider.calls) == (3 if use_tools else 2)
    late = {**params, "idempotencyKey": "too-late"}
    with pytest.raises(SteeringError):
        agent.steer_chat(late)


def _tool_agent(tmp_path, provider, *tools):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config

    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, main_config=Config(),
                      max_iterations=3, soft_warn_at_iteration=0)
    for tool in tools:
        agent.tools.register(tool)
    return agent


def _scripted(*responses):
    from flowly.providers.base import LLMProvider

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, **kwargs):
            self.calls.append(kwargs["messages"].copy())
            return responses[min(len(self.calls), len(responses)) - 1]

    return Provider()


async def _loop_with_steering(agent, journal):
    return await agent._run_llm_tool_loop(
        messages=[{"role": "system", "content": "test"}, {"role": "user", "content": "Read the fixture."}],
        session_key="chat", outbound_run_id="run", tool_platform="web", turn_messages_out=journal,
    )


@pytest.mark.asyncio
async def test_a_message_cuts_the_running_tool_and_the_agent_reads_it_at_once(tmp_path, monkeypatch):
    from flowly.agent.tools.base import Tool
    from flowly.providers.base import LLMResponse, ToolCallRequest

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    entered = asyncio.Event()
    completed = []

    class SlowTool(Tool):
        name = "steer_test_read"
        description = "Read a fixture."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            completed.append("read finished")

    provider = _scripted(
        LLMResponse(content="Reading.", tool_calls=[ToolCallRequest(id="read", name="steer_test_read", arguments={})]),
        LLMResponse(content="Stopped, as you asked."),
    )
    agent = _tool_agent(tmp_path, provider, SlowTool())
    journal = []
    with agent._steering().register("run", "chat", lambda *_: None, lambda: agent._run_aborts.preempt("run")):
        task = asyncio.create_task(_loop_with_steering(agent, journal))
        await asyncio.wait_for(entered.wait(), 5)
        agent._steering().submit(request(), lambda: {"content": "Stop, never mind"})
        # No waiting for the tool: it is cut, the run goes on with the message.
        result, *_ = await asyncio.wait_for(task, 2)
    assert result == "Stopped, as you asked."
    assert completed == []
    assert not agent.is_run_aborted("run")
    last = provider.calls[-1]
    cut = next(m for m in last if m.get("role") == "tool")
    assert cut["content"] == STEERING_INTERRUPTED_RESULT
    told = next(i for i, m in enumerate(last) if m.get("content") == steered_message_text("Stop, never mind"))
    assert last.index(cut) < told
    assert len([m for m in journal if m.get("id") == "guidance"]) == 1


@pytest.mark.asyncio
async def test_a_send_or_write_is_never_cut_halfway_and_the_next_calls_are_skipped(tmp_path, monkeypatch):
    import flowly.agent.loop as loop_module
    from flowly.agent.tools.base import Tool
    from flowly.providers.base import LLMResponse, ToolCallRequest

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(loop_module, "NON_PREEMPTIBLE_TOOLS", frozenset({"steer_test_send"}))
    entered, release = asyncio.Event(), asyncio.Event()
    ran = []

    class Send(Tool):
        name = "steer_test_send"
        description = "Send a fixture."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            entered.set()
            await release.wait()
            ran.append("sent")
            return "sent"

    class Delete(Tool):
        name = "steer_test_delete"
        description = "Delete a fixture."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            ran.append("deleted")
            return "deleted"

    provider = _scripted(
        LLMResponse(content="Working.", tool_calls=[
            ToolCallRequest(id="send", name="steer_test_send", arguments={}),
            ToolCallRequest(id="delete", name="steer_test_delete", arguments={}),
        ]),
        LLMResponse(content="Not deleting it."),
    )
    agent = _tool_agent(tmp_path, provider, Send(), Delete())
    with agent._steering().register("run", "chat", lambda *_: None, lambda: agent._run_aborts.preempt("run")):
        task = asyncio.create_task(_loop_with_steering(agent, []))
        await asyncio.wait_for(entered.wait(), 5)
        agent._steering().submit(request(), lambda: {"content": "Don't delete anything"})
        await asyncio.sleep(0.05)
        assert not task.done() and ran == [], "a send finishes before the agent reads"
        release.set()
        result, *_ = await asyncio.wait_for(task, 2)
    assert result == "Not deleting it."
    assert ran == ["sent"], "the next call never ran"
    results = {m["tool_call_id"]: m["content"] for m in provider.calls[-1] if m.get("role") == "tool"}
    assert results == {"send": "sent", "delete": STEERING_SKIPPED_RESULT}


@pytest.mark.asyncio
async def test_a_cut_command_really_dies(tmp_path, monkeypatch):
    from flowly.agent.run_abort import RunAbortController, RunAbortedError, RunPreemptedError
    from flowly.exec import ExecConfig
    from flowly.exec.approvals import ExecApprovalStore
    from flowly.exec.executor import execute_command
    from flowly.exec.types import ExecRequest

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    aborts = RunAbortController()

    async def cut(how):
        marker = tmp_path / f"{how}.txt"
        command = execute_command(ExecRequest(command=f"sleep 0.6; touch {marker}", cwd=str(tmp_path)),
                                  ExecConfig(security="full"), ExecApprovalStore())
        task = asyncio.create_task(aborts.run_cancellable(how, lambda: command, preemptible=True))
        await asyncio.sleep(0.2)
        aborts.preempt(how) if how == "message" else aborts.request(how)
        with pytest.raises(RunPreemptedError if how == "message" else RunAbortedError):
            await task
        await asyncio.sleep(0.8)
        return marker.exists()

    assert await cut("message") is False, "a message kills the command"
    assert await cut("stop") is False, "Stop kills the command"


def test_a_second_message_and_one_after_a_step_keep_no_half_text():
    from flowly.agent import inflight
    controller = RunSteeringController()
    inflight.begin("chat", "run", "Story")
    inflight.append("chat", "run", "Old")
    with controller.register("run", "chat", lambda *args: None):
        controller.observe_text("run", "Old")
        one = controller.submit(request(), lambda: {"content": "Make it shorter"})
        assert inflight.get("chat")["text"] == ""
        assert inflight.get("chat")["steeringSequence"] == 1
        params = request()
        params["idempotencyKey"] = "second"
        two = controller.submit(params, lambda: {"content": "And funnier"})
        assert (one["checkpoint"], two["checkpoint"]) == (None, None)
        assert [m["content"] for m in controller.take("run")] == ["Make it shorter", "And funnier"]
        # Returning a receipt must not expose mutable durable/controller state.
        one["sequence"] = 99
        assert controller.submit(request(), lambda: {})["sequence"] == 1
    inflight.finish("chat", "run")


# ── A message sent while its conversation's reply runs (Hakan, 2026-10-27) ──


def _wired(monkeypatch, steer, receipt=lambda *_: None):
    from flowly.channels import feature_rpc
    monkeypatch.setattr(feature_rpc, "_chat_steering_callback", steer)
    monkeypatch.setattr(feature_rpc, "_steering_receipt_lookup", receipt)
    return feature_rpc


def test_a_message_keeps_its_own_turn_when_no_reply_runs_or_it_is_not_plain_words(monkeypatch):
    from flowly.agent import inflight

    calls = []
    feature_rpc = _wired(monkeypatch, lambda params: calls.append(params) or {"runId": params["runId"]})
    plain = {"idempotencyKey": "m1", "message": "Make it shorter"}
    assert feature_rpc.steer_running_turn("web:c", plain) is None, "no reply runs"
    inflight.begin("web:c", "run-1", "Tell me a story")
    try:
        for kept in (
            {**plain, "origin": "voice"},
            {**plain, "voiceMode": True},
            {**plain, "setupAnswer": {"id": "a"}},
            {**plain, "message": "/compact"},
            {"idempotencyKey": "m1", "message": "   "},
            {"message": "no identity"},
        ):
            assert feature_rpc.steer_running_turn("web:c", kept) is None, kept
        assert calls == []
        ack = feature_rpc.steer_running_turn("web:c", plain)
        assert ack == {"accepted": True, "steered": True, "runId": "run-1", "status": "running", "messageId": "m1"}
        assert calls == [{"runId": "run-1", "sessionKey": "web:c", "idempotencyKey": "m1",
                          "message": "Make it shorter", "attachments": []}]
    finally:
        inflight.finish("web:c", "run-1")


def test_a_reply_that_is_ending_lets_the_message_take_its_own_turn(monkeypatch):
    from flowly.agent import inflight

    def refuse(params):
        raise SteeringError("RUN_NOT_ACTIVE", "ended")

    feature_rpc = _wired(monkeypatch, refuse)
    inflight.begin("web:c", "run-1", "x")
    try:
        assert feature_rpc.steer_running_turn("web:c", {"idempotencyKey": "m1", "message": "hi"}) is None
    finally:
        inflight.finish("web:c", "run-1")


@pytest.mark.asyncio
async def test_a_message_sent_during_a_reply_joins_it_and_a_resend_is_never_a_second_turn(tmp_path, monkeypatch):
    from flowly.agent import inflight
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    waiting = asyncio.Event()

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, **kwargs):
            return LLMResponse(content="ok")

        async def chat_stream(self, **kwargs):
            self.calls.append(kwargs["messages"].copy())
            if len(self.calls) == 1:
                yield LLMResponse(content="Once upon a time")
                waiting.set()
                await asyncio.Event().wait()
            else:
                yield LLMResponse(content="A short ending.", finish_reason="stop")

    provider = Provider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, main_config=Config(),
                      max_iterations=3, soft_warn_at_iteration=0)
    feature_rpc = _wired(monkeypatch, agent.steer_chat, agent.steering_receipt)

    async def chunk(text):
        pass

    inflight.begin("web:story", "story", "Tell me a story.")
    task = asyncio.create_task(agent.process_direct(
        "Tell me a story.", session_key="web:story", run_id="story", stream_callback=chunk,
        skip_memory=True, skip_context_files=True, tools_allowed=False, return_metadata=True,
    ))
    await asyncio.wait_for(waiting.wait(), 5)
    sent = {"idempotencyKey": "shorter", "message": "Make it shorter"}
    ack = feature_rpc.steer_running_turn("web:story", sent)
    assert ack["steered"] is True and ack["runId"] == "story" and ack["status"] == "running"
    # A lost acknowledgement, sent again while the reply runs: the same acceptance.
    again = feature_rpc.steer_running_turn("web:story", sent)
    assert again["replayed"] is True and again["runId"] == "story" and again["status"] == "running"
    result, _ = await asyncio.wait_for(task, 5)
    inflight.finish("web:story", "story")
    assert result == "A short ending."
    assert len(provider.calls) == 2
    assert provider.calls[1][-1] == {"role": "user", "content": steered_message_text("Make it shorter")}
    # Sent again after the reply ended: still its first acceptance, with the reply's
    # status, so the caller neither runs it again nor waits for a reply.
    late = feature_rpc.steer_running_turn("web:story", sent, lambda key, run: {"status": "completed"})
    assert late["replayed"] is True and late["runId"] == "story" and late["status"] == "completed"
    assert len(provider.calls) == 2

