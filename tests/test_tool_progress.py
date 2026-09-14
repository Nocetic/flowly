from copy import deepcopy

import pytest

from flowly.agent.tool_progress import ToolProgress
from flowly.providers.tool_preview import ToolCallPreview


def call(call_id, name="exec", arguments='{"command":"pwd"}'):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.mark.asyncio
async def test_queue_execution_and_result_are_distinct_ordered_snapshots():
    events = []

    async def publish(event):
        events.append(event)

    progress = ToolProgress("run-a", publish)
    await progress.announce(0, [call("first"), call("second")])
    first_snapshot = deepcopy(events[0])
    await progress.start("first", "exec", {"command": "pwd"})
    assert events[-1]["call"]["state"] == "running"
    assert progress.calls["second"]["state"] == "queued"
    await progress.result("first", "output", failed=False)
    await progress.start("second", "exec", {"command": "pwd"})
    await progress.result("second", "Error: fixture failure", failed=True)
    assert [e["call"]["state"] for e in events] == [
        "queued", "queued", "running", "completed", "running", "failed",
    ]
    assert [e["revision"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["state"] == "tool_progress" and e["runId"] == "run-a" for e in events)
    assert events[0] == first_snapshot
    assert events[-1]["call"]["result"] == "Error: fixture failure"
    assert events[-1]["iterationIdx"] == 0


@pytest.mark.asyncio
async def test_stop_seals_running_and_queued_calls_without_claiming_completion():
    events = []

    async def publish(event):
        events.append(event)

    progress = ToolProgress("run", publish)
    await progress.announce(7, [call("preview", "write_file"), call("queued")])
    await progress.start("preview", "write_file", {})
    await progress.settle("stopped")
    count = len(events)
    await progress.announce(7, [call("preview", "write_file"), call("queued")])
    await progress.result("preview", "late", failed=False)
    assert len(events) == count
    assert all(record["state"] == "stopped" for record in progress.calls.values())
    assert all("result" not in record for record in progress.calls.values())


@pytest.mark.asyncio
async def test_new_iterations_close_unfinished_calls_and_keep_revisions_monotonic():
    events = []

    async def publish(event):
        events.append(event)

    progress = ToolProgress("run", publish)
    await progress.announce(0, [call("abandoned", "read_file")])
    await progress.announce(1, [call("accepted")])
    assert any(e["call"]["id"] == "abandoned" and e["call"]["state"] == "stopped" for e in events)
    await progress.result("accepted", "x" * 50000, failed=False)
    result = events[-1]["call"]
    assert len(result["result"]) == 32768 and result["resultTruncated"]
    last_revision = events[-1]["revision"]
    await progress.announce(3, [call("next")])
    assert events[-1]["iterationIdx"] == 3 and events[-1]["revision"] > last_revision
    assert len(progress.calls) == 1


@pytest.mark.asyncio
async def test_failed_delivery_does_not_mutate_or_block_authoritative_state():
    async def unavailable(_):
        raise ConnectionError("fixture disconnected")

    progress = ToolProgress("run", unavailable)
    await progress.announce(0, [call("first")])
    await progress.start("first", "exec", {})
    await progress.result("first", "done", failed=False)
    assert progress.calls["first"]["state"] == "completed"


def test_inflight_replay_is_scoped_revisioned_and_detached():
    from flowly.agent import inflight

    key = "test:tool-progress"
    inflight.begin(key, "run", "question")
    inflight.append(key, "run", "Narration")
    event = {"state": "tool_progress", "iterationIdx": 0, "revision": 2,
             "runId": "run", "call": {"id": "call", "state": "running"}}
    inflight.append_tool_progress(key, "run", event)
    inflight.append_tool_progress(key, "old-run", {**event, "revision": 3})
    inflight.append_tool_progress(key, "run", {**event, "revision": 1})
    snapshot = inflight.get(key)
    assert snapshot["text"] == "Narration"
    assert snapshot["toolProgress"] == [event]
    snapshot["toolProgress"][0]["call"]["state"] = "mutated"
    assert inflight.get(key)["toolProgress"][0]["call"]["state"] == "running"
    inflight.finish(key, "run")


@pytest.mark.asyncio
async def test_real_loop_hides_partial_arguments_and_publishes_actual_sequential_execution(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.base import Tool
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    events, executed, text = [], [], []

    class Lookup(Tool):
        name = "progress_lookup"
        description = "Read fixture information."
        parameters = {"type": "object", "properties": {"value": {"type": "string"}}}

        async def execute(self, **kwargs):
            live = {e["call"]["id"]: e["call"]["state"] for e in events if e.get("state") == "tool_progress"}
            current = kwargs["value"]
            assert live[current] == "running"
            if current == "first":
                assert live["second"] == "queued"
            executed.append(current)
            return "result " + current

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="fixture")
            self.requests = 0

        def get_default_model(self):
            return "fixture"

        async def chat(self, **_):
            return LLMResponse(content="Fixture")

        async def chat_stream(self, **_):
            self.requests += 1
            if self.requests == 1:
                yield LLMResponse(content="Checking.", finish_reason="")
                yield LLMResponse(content=None, finish_reason="", tool_call_previews=[
                    ToolCallPreview(0, "first", "progress_lookup", '{"value":'),
                ])
                assert not events and not executed, "Partial arguments must not publish tool activity"
                yield LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                    ToolCallRequest("first", "progress_lookup", {"value": "first"}),
                    ToolCallRequest("second", "progress_lookup", {"value": "second"}),
                ])
            else:
                yield LLMResponse(content="Finished.", finish_reason="stop")

    agent = AgentLoop(bus=MessageBus(), provider=Provider(), workspace=tmp_path,
                      main_config=Config(), max_iterations=3, soft_warn_at_iteration=0)
    agent.tools.register(Lookup())

    async def on_iteration(event):
        events.append(event)

    async def stream(delta):
        text.append(delta)

    await agent.process_direct("Fixture question", session_key="test:progress", run_id="run",
                               stream_callback=stream, on_iteration=on_iteration, on_tool_progress=on_iteration,
                               skip_memory=True, skip_context_files=True, return_metadata=True)
    assert executed == ["first", "second"]
    assert text == ["Checking.", "Finished."]
    activity = [e for e in events if e.get("state") == "tool_progress"]
    assert [e["call"]["state"] for e in activity] == ["queued", "queued", "running", "completed", "running", "completed"]
    assert activity[-1]["call"]["state"] == "completed"
    assert activity[-1]["call"]["result"] == "result second"
    canonical = [e for e in events if e.get("role")]
    assert [e["role"] for e in canonical] == ["assistant", "tool", "tool"]
