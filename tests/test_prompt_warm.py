"""Warming a local model server's cache before the first message.

Seen on a Mac on 2026-11-06: Gemma 4 on LM Studio read the agent's fixed
prompt at 600-1000 tokens/s, so the first message after the gateway started or
the model changed waited one to two minutes before a word came back; later
messages reused the server's cache and took seconds. The warm-up sends that
prompt once in the background, built as a fresh turn builds it, asking for
one token. It must match the real turn's prompt exactly (or the cache is of no
use), never touch a cloud provider, and give way to a real request.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse


class _Server(LLMProvider):
    def __init__(self, name: str = "vllm", base: str = "http://127.0.0.1:1234/v1", *, hold: asyncio.Event | None = None):
        super().__init__(api_key="local", api_base=base)
        self.provider_name = name
        self.requests: list[dict[str, Any]] = []
        self.hold = hold
        self.cancelled = False

    def get_default_model(self) -> str:
        return "google/gemma-4-e2b"

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **kwargs) -> LLMResponse:
        self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        if self.hold is not None and max_tokens == 1:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return LLMResponse(content="ok", finish_reason="stop")


def _loop(tmp_path, monkeypatch, provider) -> AgentLoop:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, main_config=Config())


def _prefix(request: dict[str, Any]) -> tuple[str, str]:
    """What the server can cache: every system message and the tools, as the
    provider would send them."""
    systems = [
        {k: v for k, v in message.items() if not str(k).startswith("_")}
        for message in request["messages"] if message.get("role") == "system"
    ]
    return json.dumps(systems[:1], sort_keys=True), json.dumps(request["tools"], sort_keys=True)


@pytest.mark.asyncio
async def test_the_warm_up_sends_exactly_the_prompt_a_fresh_turn_sends(tmp_path, monkeypatch):
    server = _Server()
    loop = _loop(tmp_path, monkeypatch, server)
    # A real turn first, so the route (channel, tools switched off) is the one
    # the next turn will use: here it switches web_search off.
    await loop.process_direct("hello there", session_key="web:first", disabled_tools=["web_search"])
    turn = next(r for r in server.requests if r["tools"])
    assert "web_search" not in _prefix(turn)[1]

    assert await loop.schedule_prompt_warm("test") is True
    warm = server.requests[-1]

    assert warm["max_tokens"] == 1
    assert _prefix(warm) == _prefix(turn)


@pytest.mark.asyncio
async def test_after_a_restart_the_route_comes_from_disk(tmp_path, monkeypatch):
    server = _Server()
    loop = _loop(tmp_path, monkeypatch, server)
    await loop.process_direct("hello there", session_key="web:first", disabled_tools=["web_search"])
    turn = next(r for r in server.requests if r["tools"])

    restarted_server = _Server()
    restarted = _loop(tmp_path, monkeypatch, restarted_server)
    assert await restarted.schedule_prompt_warm("gateway started") is True
    assert _prefix(restarted_server.requests[-1])[1] == _prefix(turn)[1]
    stored = json.loads((tmp_path / ".flowly_state" / "warm-route.json").read_text())
    assert set(stored) == {"channel", "disabled"}  # names only, no content


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "base"), [
    ("openrouter", "https://openrouter.ai/api/v1"),
    ("anthropic", "https://api.anthropic.com/v1"),
    ("vllm", "https://my-vllm.example.com/v1"),  # self-hosted, not on this computer
])
async def test_a_model_not_on_this_computer_is_never_warmed(tmp_path, monkeypatch, name, base):
    server = _Server(name, base)
    loop = _loop(tmp_path, monkeypatch, server)
    assert loop.schedule_prompt_warm("test") is None
    assert server.requests == []


@pytest.mark.asyncio
async def test_a_real_request_waits_for_the_warm_up_and_then_uses_its_cache(tmp_path, monkeypatch):
    """Cancelling did not hand the partly read prompt over on LM Studio: the
    request landed in an empty slot and read everything again (2026-11-06)."""
    hold = asyncio.Event()
    server = _Server(hold=hold)
    loop = _loop(tmp_path, monkeypatch, server)
    task = loop.schedule_prompt_warm("test")
    await asyncio.sleep(0.05)  # the warm-up is now reading

    turn = asyncio.create_task(loop.process_direct("what's up", session_key="web:now"))
    await asyncio.sleep(0.6)
    assert [r["max_tokens"] for r in server.requests] == [1]  # the request waits

    hold.set()
    assert await turn == "ok"
    assert task.done() and not task.cancelled() and not server.cancelled
    assert server.requests[0]["max_tokens"] == 1 and server.requests[1]["max_tokens"] != 1


@pytest.mark.asyncio
async def test_a_warm_up_that_takes_too_long_is_stopped_for_the_request(tmp_path, monkeypatch):
    server = _Server(hold=asyncio.Event())  # never released
    loop = _loop(tmp_path, monkeypatch, server)
    monkeypatch.setattr(AgentLoop, "_WARM_WAIT_SECONDS", 0.3)
    task = loop.schedule_prompt_warm("test")
    await asyncio.sleep(0.05)

    assert await loop.process_direct("what's up", session_key="web:now") == "ok"
    await asyncio.sleep(0)
    assert task.cancelled() or server.cancelled


@pytest.mark.asyncio
async def test_a_refusing_server_is_only_logged(tmp_path, monkeypatch):
    class Down(_Server):
        async def chat(self, *args, **kwargs):
            raise ConnectionError("refused")

    loop = _loop(tmp_path, monkeypatch, Down())
    assert await loop.schedule_prompt_warm("test") is False


@pytest.mark.asyncio
async def test_ollama_on_this_computer_is_warmed(tmp_path, monkeypatch):
    server = _Server("ollama", "http://localhost:11434/v1")
    loop = _loop(tmp_path, monkeypatch, server)
    assert await loop.schedule_prompt_warm("test") is True
    assert server.requests[-1]["max_tokens"] == 1
