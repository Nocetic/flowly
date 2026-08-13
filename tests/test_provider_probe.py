"""The end-of-setup provider probe — one real request, an honest verdict.

The two mistakes this test file exists to prevent, both found by running the
probe against a live provider rather than reasoning about it:

1. **A failure reported as success.** OpenAI-compatible providers don't raise
   on a rejected key — they log it and return an ordinary ``LLMResponse`` with
   ``finish_reason="error"``. A probe that only catches exceptions calls a 401
   "✓ answered", which is worse than not probing.
2. **A success reported as failure.** A reasoning model can spend the whole
   token budget thinking and come back with empty content and
   ``finish_reason="length"``. The credential is fine; failing setup over an
   empty string sends the user hunting a problem that doesn't exist.
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.config.schema import Config
from flowly.integrations import provider_probe
from flowly.integrations.active_provider import ActiveProvider
from flowly.providers.base import LLMResponse


class _FakeProvider:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.agents.defaults.model = "vendor/some-model"
    return c


def _wire(monkeypatch, provider, active=True):
    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider",
        lambda config: (
            ActiveProvider(key="openrouter", api_key="k", api_base=None, source="test")
            if active else None
        ),
    )
    monkeypatch.setattr(
        "flowly.providers.factory.build_provider",
        lambda active, **kwargs: provider,
    )


def test_a_real_answer_is_success(monkeypatch, cfg):
    fake = _FakeProvider(LLMResponse(content="ready", finish_reason="stop"))
    _wire(monkeypatch, fake)
    result = provider_probe.probe_active_provider(cfg)
    assert result.ok is True
    assert result.model == "vendor/some-model"


def test_error_finish_reason_is_a_failure_not_a_pass(monkeypatch, cfg):
    """The 401-shaped case: no exception, but the call did not work."""
    fake = _FakeProvider(
        LLMResponse(
            content="Error calling LLM: Error code: 401 - {'message': 'User not found.'}",
            finish_reason="error",
        )
    )
    _wire(monkeypatch, fake)
    result = provider_probe.probe_active_provider(cfg)
    assert result.ok is False
    assert "401" in result.detail


def test_empty_content_from_a_reasoning_model_still_passes(monkeypatch, cfg):
    """Budget spent thinking → empty content, finish_reason="length". Fine."""
    fake = _FakeProvider(LLMResponse(content=None, finish_reason="length"))
    _wire(monkeypatch, fake)
    assert provider_probe.probe_active_provider(cfg).ok is True


def test_probe_gives_reasoning_models_room(monkeypatch, cfg):
    fake = _FakeProvider(LLMResponse(content="ready", finish_reason="stop"))
    _wire(monkeypatch, fake)
    provider_probe.probe_active_provider(cfg)
    assert fake.calls[0]["max_tokens"] >= 32


def test_no_provider_configured(monkeypatch, cfg):
    _wire(monkeypatch, _FakeProvider(), active=False)
    result = provider_probe.probe_active_provider(cfg)
    assert result.ok is False
    assert "no provider" in result.detail


@pytest.mark.parametrize(
    "raised,expected",
    [
        (RuntimeError("Error code: 401 - unauthorized"), "401"),
        (RuntimeError("Error code: 429 - rate limit exceeded"), "429"),
        (RuntimeError("insufficient credit on this account"), "credit"),
        (asyncio.TimeoutError(), "no answer within"),
        (RuntimeError("Connection refused"), "couldn't reach"),
    ],
)
def test_exceptions_become_one_actionable_line(monkeypatch, cfg, raised, expected):
    _wire(monkeypatch, _FakeProvider(raises=raised))
    result = provider_probe.probe_active_provider(cfg)
    assert result.ok is False
    assert expected in result.detail


def test_probe_never_raises_even_when_everything_is_broken(monkeypatch, cfg):
    def _boom(config):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider", _boom
    )
    result = provider_probe.probe_active_provider(cfg)
    assert result.ok is False
