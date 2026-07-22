"""ChatGPT Codex Responses provider (CodexResponsesProvider).

Codex credentials aren't available in CI, so these drive the pure
conversion/payload helpers directly and the SSE parser with a fake httpx
client replaying the exact frames the ChatGPT Codex backend emits — most
importantly, output items arriving in ``response.output_item.done`` while
``response.completed.output`` stays empty.
"""

import asyncio

import pytest

import flowly.providers.codex_responses_provider as codex
from flowly.providers.codex_responses_provider import (
    CodexResponsesProvider,
    _messages_to_codex_input,
    _normalize_codex_model,
    _resolve_effort,
)


@pytest.fixture(autouse=True)
def _restore_httpx():
    """Never let a fake AsyncClient leak into other test modules.

    ``_install`` swaps ``httpx.AsyncClient`` globally (``codex.httpx`` IS the
    httpx module), so without this restore the fake client would poison every
    downstream test that uses httpx.
    """
    original = codex.httpx.AsyncClient
    try:
        yield
    finally:
        codex.httpx.AsyncClient = original


# ── pure helpers ───────────────────────────────────────────────────────


def test_model_normalization_strips_prefix_and_effort_suffix():
    assert _normalize_codex_model("openai/gpt-5.5") == "gpt-5.5"
    assert _normalize_codex_model("gpt-5.5:high") == "gpt-5.5"
    assert _normalize_codex_model("gpt-5.6-sol:max") == "gpt-5.6-sol"
    assert _normalize_codex_model("chatgpt/gpt-5.4-mini") == "gpt-5.4-mini"
    assert _normalize_codex_model(None) == codex.DEFAULT_CODEX_MODEL


def test_effort_rules():
    assert _resolve_effort("gpt-5.5:high") == "high"
    assert _resolve_effort("gpt-5.5", "minimal") == "low"   # backend has no "minimal"
    assert _resolve_effort("gpt-5.5", "none") == "none"     # general model keeps none
    assert _resolve_effort("gpt-5.5-codex", "none") == "low"  # codex rejects none
    assert _resolve_effort("gpt-5.6-sol", "none") == "low"
    assert _resolve_effort("gpt-5.6-sol:max") == "max"
    assert _resolve_effort("gpt-5.5", "bogus") == "medium"


def test_system_message_becomes_instructions_and_ids_are_stripped():
    instr, items = _messages_to_codex_input([
        {"role": "system", "content": "You are Flowly."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello",
         "tool_calls": [{"id": "call_1", "function": {"name": "foo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ])
    assert instr == "You are Flowly."
    # first item is the user turn (system went to instructions, not input)
    assert items[0] == {"role": "user", "content": "hi"}
    fc = next(i for i in items if i.get("type") == "function_call")
    assert fc["call_id"] == "call_1" and fc["name"] == "foo"
    out = next(i for i in items if i.get("type") == "function_call_output")
    assert out["call_id"] == "call_1" and out["output"] == "result"
    # stateless mode: no bare "id" fields leak through
    assert all("id" not in i for i in items)


def test_payload_shape_is_codex_backend_compatible():
    prov = CodexResponsesProvider(api_key="tok", account_id="acct-1")
    payload, sid = prov._build_payload(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "yo"}],
        None, "gpt-5.5", "auto", stream=True,
    )
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == "sys"
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"]["effort"] in codex._VALID_EFFORTS
    # The backend rejects these — they must never be sent.
    assert "max_output_tokens" not in payload
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert isinstance(sid, str) and len(sid) == 32


def test_headers_carry_account_id_and_beta():
    prov = CodexResponsesProvider(api_key="tok", account_id="acct-9")
    h = prov._headers("sess-1", accept="text/event-stream")
    assert h["Authorization"] == "Bearer tok"
    assert h["ChatGPT-Account-Id"] == "acct-9"
    assert h["OpenAI-Beta"] == "responses=experimental"
    assert h["session_id"] == "sess-1"


# ── SSE parsing with a fake client ─────────────────────────────────────


class _FakeStream:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeClient:
    def __init__(self, lines, status=200):
        self._lines = lines
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return _FakeStream(self._status, self._lines)


def _provider():
    return CodexResponsesProvider(api_key="k", account_id="acct-1", default_model="gpt-5.5")


def _install(lines, status=200):
    codex.httpx.AsyncClient = lambda **kw: _FakeClient(lines, status)  # type: ignore


# The critical backend quirk: items arrive in output_item.done; completed.output is [].
_TEXT_FRAMES = [
    'data: {"type":"response.output_text.delta","delta":"Hel"}',
    'data: {"type":"response.output_text.delta","delta":"lo"}',
    'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant",'
    '"content":[{"type":"output_text","text":"Hello"}]}}',
    'data: {"type":"response.completed","response":{"status":"completed","output":[],'
    '"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
    "",
]

_TOOL_FRAMES = [
    'data: {"type":"response.output_item.done","item":{"type":"function_call",'
    '"name":"get_weather","arguments":"{\\"city\\":\\"Istanbul\\"}","call_id":"call_abc"}}',
    'data: {"type":"response.completed","response":{"status":"completed","output":[],'
    '"usage":{"input_tokens":10,"output_tokens":4,"total_tokens":14}}}',
    "",
]


def test_blocking_chat_collects_message_from_output_item_done():
    prov = _provider()
    _install(_TEXT_FRAMES)
    resp = asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))
    assert resp.content == "Hello"
    assert resp.finish_reason == "stop"
    assert resp.usage.get("total_tokens") == 7


def test_blocking_chat_collects_tool_call_from_output_item_done():
    prov = _provider()
    _install(_TOOL_FRAMES)
    resp = asyncio.run(prov.chat(messages=[{"role": "user", "content": "weather?"}]))
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "get_weather"
    assert tc.arguments == {"city": "Istanbul"}
    assert tc.id == "call_abc"


def test_stream_yields_deltas_then_final():
    prov = _provider()
    _install(_TEXT_FRAMES)
    chunks = []

    async def go():
        async for ch in prov.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(ch)

    asyncio.run(go())
    text = "".join(c.content for c in chunks if c.content and c.finish_reason == "")
    assert text == "Hello"
    final = chunks[-1]
    assert final.content is None
    assert final.finish_reason == "stop"


def test_stream_tool_call_in_final():
    prov = _provider()
    _install(_TOOL_FRAMES)
    chunks = []

    async def go():
        async for ch in prov.chat_stream(messages=[{"role": "user", "content": "weather?"}]):
            chunks.append(ch)

    asyncio.run(go())
    final = chunks[-1]
    assert final.finish_reason == "tool_calls"
    assert final.tool_calls[0].name == "get_weather"


def test_http_error_returns_error_response_not_raise():
    prov = _provider()
    _install(['data: {"type":"noise"}', ""], status=400)
    resp = asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))
    assert resp.finish_reason == "error"
    assert "Error calling LLM" in (resp.content or "")


def test_default_model_matches_curated_default():
    # The curated cascade default must line up with the provider default so a
    # provider switch doesn't land on an id the backend rejects.
    from flowly.integrations.active_provider import DEFAULT_MODELS
    assert DEFAULT_MODELS["openai_codex"] == codex.DEFAULT_CODEX_MODEL


def test_openai_codex_compatibility_accepts_new_gpt_5_variants():
    from flowly.integrations.active_provider import model_fits_provider

    assert model_fits_provider("gpt-5.6-sol", "openai_codex") is True
    assert model_fits_provider("gpt-5.6-terra", "openai_codex") is True
    assert model_fits_provider("gpt-4o", "openai_codex") is False
