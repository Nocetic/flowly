from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.agent import inflight
from flowly.agent.run_abort import RunAbortController, RunAbortedError
from flowly.gateway.server import GatewayServer


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_abort_controller_cancels_only_the_active_run_operation() -> None:
    controller = RunAbortController()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_operation() -> str:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(
        controller.run_cancellable("run-a", long_operation)
    )
    await started.wait()

    assert controller.request("run-a") is True
    with pytest.raises(RunAbortedError):
        await task
    assert cancelled.is_set()
    assert controller.is_requested("run-a") is True
    assert controller.is_requested("run-b") is False


@pytest.mark.asyncio
async def test_abort_controller_does_not_start_operation_after_stop() -> None:
    controller = RunAbortController()
    called = False

    async def operation() -> str:
        nonlocal called
        called = True
        return "unexpected"

    controller.request("run-a")
    with pytest.raises(RunAbortedError):
        await controller.run_cancellable("run-a", operation)
    assert called is False


@pytest.mark.asyncio
async def test_gateway_abort_uses_cooperative_callback_without_cancelling_turn() -> None:
    server = object.__new__(GatewayServer)
    observed: list[str] = []

    def on_chat_abort(run_id: str) -> bool:
        observed.append(run_id)
        return True

    server.on_chat_abort = on_chat_abort
    server._ws_rpc_reply = AsyncMock()

    sleeper = asyncio.create_task(asyncio.sleep(60))
    server._active_tasks = {"run-a": sleeper}
    ws = object()
    try:
        await server._ws_rpc_chat_abort(ws, "rpc-1", {"runId": "run-a"})
        assert observed == ["run-a"]
        assert sleeper.cancelled() is False
        server._ws_rpc_reply.assert_awaited_once_with(
            ws,
            "rpc-1",
            {"ok": True, "cancelled": True},
        )
    finally:
        sleeper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sleeper


@pytest.mark.asyncio
async def test_gateway_emits_one_aborted_final_with_partial_and_duration() -> None:
    server = object.__new__(GatewayServer)
    server._session_ws = {}
    server._schedule_offline_chat_push = lambda *args: None

    async def on_chat_message(*args: Any) -> tuple[str, dict[str, Any]]:
        return "partial answer", {
            "aborted": True,
            "duration_ms": 1234,
            "usage": {},
            "model": "test/model",
        }

    server.on_chat_message = on_chat_message
    ws = _FakeWS()
    inflight._runs.clear()

    await server._run_chat(
        ws,
        "client",
        "desktop:chat",
        "hello",
        "run-a",
        stream_callback=AsyncMock(),
    )

    assert len(ws.sent) == 1
    data = ws.sent[0]["data"]
    assert data["state"] == "final"
    assert data["runId"] == "run-a"
    assert data["aborted"] is True
    assert data["durationMs"] == 1234
    assert data["message"]["content"] == [
        {"type": "text", "text": "partial answer"}
    ]
