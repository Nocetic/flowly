"""A local model server's real context, kept current between turns
(flowly/integrations/local_window.py).

Seen on a Mac on 2026-11-06: LM Studio held gemma-4-e2b at 8K when the agent
switched to it, the owner reloaded it at 128K, and the agent still refused
every message as too large until the gateway restarted, because the window
was read once, when the provider was switched. The other way round, a model
LM Studio unloaded was loaded again by LM Studio on the next request with its
own small default.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from flowly.compaction import window_need
from flowly.integrations import local_models, local_window, model_catalog
from flowly.integrations.local_window import LocalWindowSync

MODEL = "google/gemma-4-e2b"
LMS_BASE = "http://127.0.0.1:1234/v1"


@pytest.fixture(autouse=True)
def _clean():
    model_catalog.flush_cache()
    window_need.reset_for_tests()
    local_models._SEEN_CONTEXT.clear()
    window_need.record_fixed_overhead(68_013)  # this agent needs 96,256
    yield
    model_catalog.flush_cache()
    window_need.reset_for_tests()
    local_models._SEEN_CONTEXT.clear()


class _Provider:
    def __init__(self, name: str = "vllm", base: str | None = LMS_BASE):
        self.provider_name = name
        self.api_base = base


class _LMStudio:
    def __init__(self, contexts: tuple[int, ...] = (), *, load_status: int = 200):
        self.contexts = list(contexts)
        self.load_status = load_status
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [{
                "type": "llm", "key": MODEL, "max_context_length": 131072,
                "capabilities": {"trained_for_tool_use": True},
                "loaded_instances": [{"id": MODEL, "config": {"context_length": c}} for c in self.contexts],
            }]})
        if request.url.path == "/api/v1/models/unload":
            self.contexts = []
            return httpx.Response(200, json={"instance_id": MODEL})
        if request.url.path == "/api/v1/models/load":
            if self.load_status >= 400:
                return httpx.Response(self.load_status, json={"error": {"message": "Insufficient memory"}})
            context = json.loads(request.content)["context_length"]
            self.contexts = [context]
            return httpx.Response(200, json={"status": "loaded", "load_config": {"context_length": context}})
        return httpx.Response(404)


def _patch_http(monkeypatch, app) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(app)))


def _sync(clock=None) -> LocalWindowSync:
    return LocalWindowSync(clock=clock or (lambda: 1000.0))


def test_a_model_lm_studio_unloaded_is_loaded_with_enough_before_the_turn(monkeypatch):
    app = _LMStudio()
    _patch_http(monkeypatch, app)

    context = asyncio.run(_sync().before_request(_Provider(), MODEL, reserve=20_000))

    assert context == 98_304
    assert "POST /api/v1/models/load" in app.calls
    # Compaction reads the window from here.
    assert model_catalog.get_context_window(MODEL) == 98_304


def test_a_model_reloaded_with_more_is_seen_without_a_restart(monkeypatch):
    """The case hit: switched at 8K, reloaded by the owner at 128K."""
    app = _LMStudio((8192,))
    _patch_http(monkeypatch, app)
    sync = _sync()
    # Held at 8K: Flowly loads it again with enough.
    assert asyncio.run(sync.before_request(_Provider(), MODEL, reserve=20_000)) == 98_304
    # The owner reloads it at 128K in LM Studio.
    app.contexts = [131072]
    app.calls.clear()
    assert asyncio.run(sync.before_request(_Provider(), MODEL, reserve=20_000, force=True)) == 131072
    assert model_catalog.get_context_window(MODEL) == 131072
    assert not any(call.startswith("POST") for call in app.calls)


def test_the_server_is_asked_at_most_every_thirty_seconds_unless_forced(monkeypatch):
    app = _LMStudio((131072,))
    _patch_http(monkeypatch, app)
    now = [1000.0]
    sync = _sync(clock=lambda: now[0])
    provider = _Provider()

    asyncio.run(sync.before_request(provider, MODEL, reserve=20_000))
    asked = len(app.calls)
    now[0] += 10
    assert asyncio.run(sync.before_request(provider, MODEL, reserve=20_000)) is None
    assert len(app.calls) == asked
    asyncio.run(sync.before_request(provider, MODEL, reserve=20_000, force=True))
    assert len(app.calls) > asked
    asked = len(app.calls)
    now[0] += local_window.CHECK_EVERY_SECONDS + 1
    asyncio.run(sync.before_request(provider, MODEL, reserve=20_000))
    assert len(app.calls) > asked


@pytest.mark.parametrize("provider", [
    _Provider("openrouter", "https://openrouter.ai/api/v1"),
    _Provider("flowly", "http://localhost:3000/api/v1"),
    _Provider("vllm", "https://my-vllm.example.com/v1"),
    _Provider("vllm", None),
    object(),
])
def test_nothing_but_a_local_server_is_asked(monkeypatch, provider):
    def refuse(request):  # pragma: no cover - must not be called
        raise AssertionError(f"unexpected request {request.url}")

    _patch_http(monkeypatch, refuse)
    assert asyncio.run(_sync().before_request(provider, MODEL, reserve=20_000)) is None


def test_ollama_is_read_not_changed(monkeypatch):
    calls: list[str] = []

    def ollama(request):
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b", "context_length": 131072}]})
        return httpx.Response(404)

    _patch_http(monkeypatch, ollama)
    provider = _Provider("ollama", "http://127.0.0.1:11434/v1")
    assert asyncio.run(_sync().before_request(provider, "qwen3:8b", reserve=20_000)) == 131072
    assert calls == ["GET /api/ps"]
    assert model_catalog.get_context_window("qwen3:8b") == 131072


def test_a_failed_check_never_breaks_the_turn(monkeypatch):
    _patch_http(monkeypatch, _LMStudio(load_status=500))
    assert asyncio.run(_sync().before_request(_Provider(), MODEL, reserve=20_000)) is None

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    _patch_http(monkeypatch, down)
    assert asyncio.run(_sync().before_request(_Provider(), MODEL, reserve=20_000, force=True)) is None


def test_a_vllm_server_that_is_not_lm_studio_is_left_alone(monkeypatch):
    calls: list[str] = []

    def vllm(request):
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(404)

    _patch_http(monkeypatch, vllm)
    provider = _Provider("vllm", "http://127.0.0.1:8000/v1")
    assert asyncio.run(_sync().before_request(provider, "meta-llama/Llama-3.1-8B-Instruct", reserve=20_000)) is None
    assert calls == ["GET /api/v1/models"]


def test_the_runtime_window_outranks_the_catalogue_and_goes_with_a_flush():
    model_catalog._CACHE["vllm"] = [model_catalog.Model(id=MODEL, name=MODEL, context_window=8192)]
    assert model_catalog.get_context_window(MODEL) == 8192
    model_catalog.set_runtime_context_window(MODEL, 98_304)
    assert model_catalog.get_context_window(MODEL) == 98_304
    # The picker's list is untouched.
    assert [m.id for m in model_catalog._CACHE["vllm"]] == [MODEL]
    model_catalog.flush_cache()
    assert model_catalog.get_context_window(MODEL) is None


# ── in the agent loop ───────────────────────────────────────────────────────

from dataclasses import dataclass, field

from flowly.agent.loop import AgentLoop
from flowly.agent.run_abort import RunAbortController
from flowly.compaction.request_guard import ProviderRequestCoordinator
from flowly.compaction.service import CompactionService
from flowly.compaction.types import CompactionConfig, MemoryFlushConfig
from flowly.providers.base import LLMResponse


class _LocalProvider:
    provider_name = "vllm"
    api_base = LMS_BASE

    def __init__(self):
        self.sent: list[dict] = []

    async def chat(self, **kwargs):
        self.sent.append(kwargs)
        return LLMResponse(content="Merhaba!", finish_reason="stop")


@dataclass
class _Loop:
    provider: object
    compaction: CompactionService
    _local_window: LocalWindowSync = field(default_factory=lambda: LocalWindowSync())

    _chat_without_stream = AgentLoop._chat_without_stream
    _chat_with_stream = AgentLoop._chat_with_stream
    _request_coordinator = AgentLoop._request_coordinator
    _request_too_large_response = staticmethod(AgentLoop._request_too_large_response)
    _call_provider_with_context_recovery = AgentLoop._call_provider_with_context_recovery
    is_run_aborted = AgentLoop.is_run_aborted
    _steering = AgentLoop._steering

    def __post_init__(self):
        self._provider_requests = ProviderRequestCoordinator(self.compaction)
        self._run_aborts = RunAbortController()
        self._api_call_count = 0

    def _touch_activity(self, _detail: str) -> None:
        pass


def _loop() -> tuple[_Loop, _LocalProvider]:
    provider = _LocalProvider()
    compaction = CompactionService(
        provider=provider, model=MODEL,
        config=CompactionConfig(mode="default", reserve_tokens_floor=20_000,
                                memory_flush=MemoryFlushConfig(enabled=False)),
    )
    return _Loop(provider, compaction), provider


def _request():
    tools = [{"type": "function", "function": {"name": f"tool_{i}", "description": "d" * 3_000, "parameters": {}}}
             for i in range(12)]
    return [{"role": "system", "content": "s" * 40_000}, {"role": "user", "content": "selam"}], tools


def _call(loop: _Loop):
    messages, tools = _request()
    return asyncio.run(loop._call_provider_with_context_recovery(
        messages=messages, tools=tools, model=MODEL, temperature=0.7, tool_choice="auto", session_key="web:x"))


def test_a_stale_window_is_checked_again_and_the_message_goes_through(monkeypatch):
    """Sized against 8K from when the agent switched; the owner has since
    reloaded the model at 128K. The refusal triggers a check, then it fits."""
    loop, provider = _loop()
    app = _LMStudio((131072,))
    _patch_http(monkeypatch, app)
    model_catalog.set_runtime_context_window(MODEL, 8192)
    # Asked a moment ago: the routine check before the request is skipped.
    loop._local_window._checked[("vllm", LMS_BASE, MODEL)] = loop._local_window._clock()

    response, _ = _call(loop)

    assert response.content == "Merhaba!" and len(provider.sent) == 1
    assert model_catalog.get_context_window(MODEL) == 131072


def test_a_window_really_too_small_is_still_refused_with_the_numbers(monkeypatch):
    from flowly.agent.error_classifier import present_provider_error

    loop, provider = _loop()
    # LM Studio cannot load it with more (the model's own window is 8K).
    def tiny(request):
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [{
                "type": "llm", "key": MODEL, "max_context_length": 8192,
                "loaded_instances": [{"id": MODEL, "config": {"context_length": 8192}}]}]})
        return httpx.Response(404)

    _patch_http(monkeypatch, tiny)
    model_catalog.set_runtime_context_window(MODEL, 8192)

    response, _ = _call(loop)

    assert provider.sent == []
    shown = present_provider_error(response)
    assert shown.code == "MODEL_CONTEXT_WINDOW_TOO_SMALL" and "8,192" in shown.message
