"""Use the real provider and SDK against an in-memory HTTP/SSE boundary."""
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from flowly.agent.loop import AgentLoop
from flowly.agent.run_abort import RunAbortController
from flowly.agent.run_steering import RunSteeringController
from flowly.providers.openrouter_provider import OpenRouterProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_delivery", [False, True])
@pytest.mark.parametrize("silent_continuation", [False, True])
async def test_steer_closes_interrupted_http_stream_before_the_next_request(monkeypatch, cancel_during_delivery, silent_continuation):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            payload = {"id": "fixture", "choices": [{"index": 0, "delta": {"content": "Partial story"}, "finish_reason": None}]}
            yield ("data: " + json.dumps(payload) + "\n\n").encode()
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    continuation_entered, continuation_closed = asyncio.Event(), asyncio.Event()

    class SilentStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            continuation_entered.set()
            await asyncio.Event().wait()
            yield b""  # async generator protocol; the provider never produces a token

        async def aclose(self):
            continuation_closed.set()

    calls = []
    async def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        assert closed.is_set(), "The old HTTP response must close before guidance starts another request"
        if silent_continuation:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SilentStream())
        payload = {"id": "continued", "choices": [{"index": 0, "delta": {"content": "Understood."}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", http_client=http, max_retries=0)
        provider = OpenRouterProvider(api_key="fixture", api_base="https://fixture.invalid/v1")
        monkeypatch.setattr(provider, "_client_for_key", lambda _: client)
        controller = RunSteeringController()
        text = []
        agent = AgentLoop.__new__(AgentLoop)
        agent.provider = provider
        agent._run_aborts = RunAbortController()
        agent._run_steering = controller
        agent._touch_activity = lambda *_: None
        async def deliver(chunk):
            text.append(chunk)
            if cancel_during_delivery and len(calls) == 1:
                entered.set()
                await asyncio.Event().wait()
        async def consume():
            return await agent._chat_with_stream(
                messages=[{"role": "user", "content": "Fixture only"}], tools=None,
                model="fixture", temperature=0, tool_choice="auto", stream_callback=deliver,
                run_id="run",
            )
        with controller.register("run", "chat", lambda *_: None):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 2)
            controller.submit(dict(runId="run", sessionKey="chat", idempotencyKey="guidance", message="Stop the story"), lambda: {"content": "Stop the story"})
            response = await asyncio.wait_for(task, 2)
            assert response.finish_reason == "steered"
            assert closed.is_set(), "Steer leaked the cancelled SDK/HTTP response"
            controller.take("run")
            continuation = asyncio.create_task(consume())
            if silent_continuation:
                await asyncio.wait_for(continuation_entered.wait(), 2)
                agent.mark_aborted("run")
            result = await asyncio.wait_for(continuation, 2)
            if silent_continuation:
                assert result.finish_reason == "aborted"
                assert continuation_closed.is_set()
        assert text == (["Partial story"] if silent_continuation else ["Partial story", "Understood."])
