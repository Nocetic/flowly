"""Tests for MCP sampling (Faz 3d, P1/P2).

Drives the SamplingHandler with a mocked provider so no LLM is called.
Covers the happy path, rate limit, model allowlist, token cap, and
message conversion.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import flowly.mcp.sampling as sampling

pytestmark = pytest.mark.skipif(
    not sampling._SAMPLING_TYPES, reason="mcp sampling types unavailable",
)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeProvider:
    def __init__(self):
        self.calls = []

    async def chat(self, *, messages, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        return _FakeResponse("sampled reply")


def _params(messages, *, max_tokens=1000, system=None, model_hint=None):
    msgs = [
        SimpleNamespace(role=r, content=SimpleNamespace(type="text", text=t))
        for r, t in messages
    ]
    prefs = None
    if model_hint:
        prefs = SimpleNamespace(hints=[SimpleNamespace(name=model_hint)])
    return SimpleNamespace(
        messages=msgs, maxTokens=max_tokens, systemPrompt=system,
        modelPreferences=prefs, temperature=0.5,
    )


@pytest.fixture
def patched_provider(monkeypatch):
    prov = _FakeProvider()
    monkeypatch.setattr(sampling, "_build_provider", lambda model: prov)
    return prov


def test_happy_path_text_completion(patched_provider):
    h = sampling.SamplingHandler("srv", {"enabled": True, "model": "x/y"})
    result = asyncio.run(h(None, _params([("user", "hello")])))
    # Returns a CreateMessageResult with the provider's text.
    assert getattr(result.content, "text", None) == "sampled reply"
    assert result.role == "assistant"
    assert patched_provider.calls[0]["model"] == "x/y"


def test_system_prompt_and_messages_converted(patched_provider):
    h = sampling.SamplingHandler("srv", {"enabled": True, "model": "x/y"})
    asyncio.run(h(None, _params([("user", "hi"), ("assistant", "yo")], system="be terse")))
    msgs = patched_provider.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1]["content"] == "hi"
    assert msgs[2]["content"] == "yo"


def test_token_cap_clamps(patched_provider):
    h = sampling.SamplingHandler("srv", {"enabled": True, "model": "x/y", "max_tokens_cap": 100})
    asyncio.run(h(None, _params([("user", "hi")], max_tokens=99999)))
    assert patched_provider.calls[0]["max_tokens"] == 100


def test_rate_limit_blocks(patched_provider):
    h = sampling.SamplingHandler("srv", {"enabled": True, "model": "x/y", "max_rpm": 2})
    asyncio.run(h(None, _params([("user", "1")])))
    asyncio.run(h(None, _params([("user", "2")])))
    result = asyncio.run(h(None, _params([("user", "3")])))
    # Third within the window → ErrorData, provider not called a 3rd time.
    assert getattr(result, "message", "").lower().count("rate limit") == 1
    assert len(patched_provider.calls) == 2


def test_model_allowlist_rejects(patched_provider):
    h = sampling.SamplingHandler(
        "srv", {"enabled": True, "model": "blocked/model", "allowed_models": ["ok/model"]},
    )
    result = asyncio.run(h(None, _params([("user", "hi")])))
    assert "allowed_models" in getattr(result, "message", "")
    assert patched_provider.calls == []


def test_model_hint_used_when_no_override(patched_provider):
    h = sampling.SamplingHandler("srv", {"enabled": True})
    asyncio.run(h(None, _params([("user", "hi")], model_hint="hinted/model")))
    assert patched_provider.calls[0]["model"] == "hinted/model"


def test_build_callback_gating():
    assert sampling.build_sampling_callback("s", {"enabled": False}) is None
    assert sampling.build_sampling_callback("s", {}) is None
    cb = sampling.build_sampling_callback("s", {"enabled": True})
    assert cb is not None


def test_provider_error_returns_errordata(monkeypatch):
    class _BoomProvider:
        async def chat(self, **kw):
            raise RuntimeError("upstream 500")
    monkeypatch.setattr(sampling, "_build_provider", lambda model: _BoomProvider())
    h = sampling.SamplingHandler("srv", {"enabled": True, "model": "x/y"})
    result = asyncio.run(h(None, _params([("user", "hi")])))
    assert "LLM call failed" in getattr(result, "message", "")
