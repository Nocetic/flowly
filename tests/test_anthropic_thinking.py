"""Claude's thinking on, off and its level with an Anthropic key.

Before: Core sent only ``output_config.effort``, never switched thinking on or
off, always sent ``temperature`` (a 400 on Opus 4.7 and later, and on any
value but the default on Sonnet 5.5 and Haiku 5.5), and dropped the thinking
blocks Claude returned, so its reasoning was lost between tool calls. Now each
family is switched the way its docs say, ``temperature`` goes only where it is
taken, the blocks are kept with the message in the order they came and sent
back, and blocks the API refuses are left out and the request sent again.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import flowly.integrations.model_catalog as catalog
from flowly.compaction.estimator import estimate_message_tokens
from flowly.providers import anthropic_provider as mod
from flowly.providers.anthropic_provider import AnthropicProvider
from flowly.providers.base import PROVIDER_REPLAY_KEY

MODEL = "claude-opus-5"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mod, "_REJECTED_THINKING", set())
    monkeypatch.setattr(catalog, "_CACHE", {})


def _provider(effort: str = "", base: str | None = None) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="sk-ant-test", api_base=base)
    provider.reasoning_effort = effort
    return provider


def _payload(model: str, effort: str, messages: list[dict] | None = None, base: str | None = None) -> dict:
    return _provider(effort, base)._build_payload(
        messages=messages or [{"role": "user", "content": "hi"}], tools=None, model=model,
        max_tokens=100, temperature=0.7, tool_choice=None, effort=effort,
    )


# ── switching ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("model", "off"), [
    ("claude-sonnet-5-5", {"type": "between_tools"}),  # disabled is a 400 there
    ("claude-opus-5", {"type": "disabled"}),
    ("claude-sonnet-5", {"type": "disabled"}),
    ("claude-haiku-5-5", {"type": "disabled"}),
    ("claude-opus-4-8", {"type": "disabled"}),
    ("claude-sonnet-4-6", {"type": "disabled"}),
])
def test_off_is_each_familys_own_and_carries_no_effort(model, off):
    payload = _payload(model, "none")
    assert payload["thinking"] == off
    # Opus 5 and Haiku 5.5 take disabled only at effort high or lower.
    assert "output_config" not in payload


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1"])
def test_a_model_that_always_thinks_runs_at_its_weakest_level_when_off(model):
    payload = _payload(model, "none")
    assert "thinking" not in payload and payload["output_config"] == {"effort": "low"}


def test_on_is_adaptive_where_the_model_does_not_think_unasked():
    assert _payload("claude-opus-4-7", "max")["thinking"] == {"type": "adaptive"}
    assert _payload("claude-sonnet-4-6", "medium")["thinking"] == {"type": "adaptive"}
    # Opus 5 thinks by default: the level alone.
    on = _payload(MODEL, "xhigh")
    assert "thinking" not in on and on["output_config"] == {"effort": "xhigh"}


@pytest.mark.parametrize(("model", "effort", "temperature"), [
    ("claude-opus-4-7", "", False),  # refused outright
    ("claude-opus-5", "none", False),
    ("claude-sonnet-5-5", "none", False),  # only the default is taken
    ("claude-haiku-5-5", "", False),
    ("claude-opus-6", "", False),  # a newer model nobody listed yet
    ("claude-sonnet-4-6", "none", True),
    ("claude-sonnet-4-6", "high", False),  # thinking takes none
    ("claude-opus-4-5", "high", True),
    ("claude-haiku-4-5", "", True),
])
def test_temperature_goes_only_where_it_is_taken(model, effort, temperature):
    assert ("temperature" in _payload(model, effort)) is temperature


async def test_the_catalogue_shows_thinking_off_where_the_model_does_not_think_unasked(monkeypatch):
    from flowly.integrations import models_dev

    async def rows(_key):
        return [catalog.Model(id=m, name=m) for m in ("claude-opus-4-7", "claude-opus-5", "claude-opus-5-5")]

    monkeypatch.setattr(models_dev, "fetch_provider_models", rows)
    by_id = {m.id: m for m in await catalog.fetch_models("anthropic")}
    assert by_id["claude-opus-4-7"].efforts[0] == "none" and by_id["claude-opus-4-7"].default_thinking is False
    assert by_id["claude-opus-5"].default_thinking is True
    assert "none" not in by_id["claude-opus-5-5"].efforts


# ── keeping and sending back the blocks ───────────────────────────────────

THINK_1 = {"type": "thinking", "thinking": "Read the file first.", "signature": "sig-1"}
THINK_2 = {"type": "thinking", "thinking": "Now list it.", "signature": "sig-2"}
REDACTED = {"type": "redacted_thinking", "data": "opaque"}
ANSWER = {
    "model": MODEL, "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5},
    "content": [
        THINK_1,
        {"type": "text", "text": "Looking."},
        {"type": "tool_use", "id": "toolu_a", "name": "read", "input": {"path": "x"}},
        THINK_2, REDACTED,
        {"type": "tool_use", "id": "toolu_b", "name": "ls", "input": {}},
    ],
}


def _history(envelope: dict, model_text: str = "Looking.") -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": model_text, PROVIDER_REPLAY_KEY: envelope, "tool_calls": [
            {"id": "toolu_a", "type": "function", "function": {"name": "read", "arguments": '{"path": "x"}'}},
            {"id": "toolu_b", "type": "function", "function": {"name": "ls", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_a", "content": "file"},
        {"role": "tool", "tool_call_id": "toolu_b", "content": "dir"},
    ]


def _envelope() -> dict:
    return _provider()._parse_response(ANSWER, MODEL).provider_replay


def test_the_blocks_are_kept_in_the_order_they_came():
    envelope = _envelope()
    assert envelope["provider"] == "anthropic" and envelope["model"] == MODEL
    assert envelope["items"] == [THINK_1, THINK_2, REDACTED]
    assert envelope["layout"] == ["t", "x", "u:toolu_a", "t", "t", "u:toolu_b"]


def test_they_go_back_where_they_were_written():
    payload = _payload(MODEL, "high", _history(_envelope()))
    assistant = payload["messages"][1]["content"]
    assert [block["type"] for block in assistant] == [
        "thinking", "text", "tool_use", "thinking", "redacted_thinking", "tool_use"]
    assert assistant[0] == THINK_1 and assistant[4] == REDACTED


@pytest.mark.parametrize(("model", "effort", "base"), [
    ("claude-opus-5-5", "high", None),  # another model's blocks
    (MODEL, "none", None),  # thinking off
    (MODEL, "high", "https://my-proxy.example.com/v1"),  # nobody there checks them
])
def test_they_go_nowhere_else(model, effort, base):
    payload = _payload(model, effort, _history(_envelope()), base)
    kinds = [block["type"] for block in payload["messages"][1]["content"]]
    assert "thinking" not in kinds and "redacted_thinking" not in kinds
    assert kinds == ["text", "tool_use", "tool_use"]


def test_a_response_without_blocks_keeps_nothing():
    plain = {"model": MODEL, "stop_reason": "end_turn", "content": [{"type": "text", "text": "Hi"}]}
    assert _provider()._parse_response(plain, MODEL).provider_replay == {}


def test_compaction_counts_the_thinking_sent_back():
    message = _history(_envelope())[1]
    bare = {k: v for k, v in message.items() if k != PROVIDER_REPLAY_KEY}
    assert estimate_message_tokens(message) > estimate_message_tokens(bare)


# ── a refused block ───────────────────────────────────────────────────────

REFUSAL = {"type": "error", "error": {"type": "invalid_request_error", "message":
           "messages.1.content.0: Invalid `signature` in `thinking` block. "
           "The block is bound to a different conversation."}}


def _patch_http(monkeypatch, handler) -> list[dict]:
    sent: list[dict] = []
    real = httpx.AsyncClient

    def app(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return handler(body)

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(app)))
    return sent


def _thinking_in(body: dict) -> int:
    return sum(
        block.get("type") in ("thinking", "redacted_thinking")
        for message in body["messages"] for block in message["content"]
    )


def _refuse_blocks(answer: httpx.Response):
    def handler(body):
        return httpx.Response(400, json=REFUSAL) if _thinking_in(body) else answer
    return handler


async def test_refused_blocks_are_left_out_sent_again_and_remembered(monkeypatch):
    done = {"model": MODEL, "stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]}
    sent = _patch_http(monkeypatch, _refuse_blocks(httpx.Response(200, json=done)))
    provider = _provider("high")
    history = _history(_envelope())

    response = await provider.chat(messages=history, model=MODEL)

    assert response.content == "Done."
    assert [_thinking_in(body) for body in sent] == [3, 0]
    # Remembered: the next request leaves them out from the start.
    await provider.chat(messages=history, model=MODEL)
    assert _thinking_in(sent[2]) == 0 and len(sent) == 3


async def test_another_refusal_is_not_retried(monkeypatch):
    sent = _patch_http(monkeypatch, lambda body: httpx.Response(400, json={
        "type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: too large"}}))
    response = await _provider("high").chat(messages=_history(_envelope()), model=MODEL)
    assert response.finish_reason == "error" and len(sent) == 1


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = _sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Plan "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "it."}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-s"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
    {"type": "message_stop"},
)


async def test_a_streamed_answer_keeps_its_blocks_and_a_refusal_is_sent_again(monkeypatch):
    sent = _patch_http(monkeypatch, _refuse_blocks(
        httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})))
    out = [chunk async for chunk in _provider("high").chat_stream(messages=_history(_envelope()), model=MODEL)]

    assert "".join(chunk.content or "" for chunk in out) == "Hi"
    final = out[-1]
    assert final.finish_reason == "stop"
    assert final.provider_replay["items"] == [{"type": "thinking", "thinking": "Plan it.", "signature": "sig-s"}]
    assert final.provider_replay["layout"] == ["t", "x"]
    assert [_thinking_in(body) for body in sent] == [3, 0]
