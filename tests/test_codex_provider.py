"""ChatGPT Codex Responses provider (CodexResponsesProvider).

Codex credentials aren't available in CI, so these drive the pure
conversion/payload helpers directly and the SSE parser with a fake httpx
client replaying the exact frames the ChatGPT Codex backend emits — most
importantly, output items arriving in ``response.output_item.done`` while
``response.completed.output`` stays empty.
"""

import asyncio
from types import SimpleNamespace

import pytest

import flowly.providers.codex_responses_provider as codex
from flowly.integrations.active_provider import ActiveProvider
from flowly.providers.base import PROVIDER_REPLAY_KEY
from flowly.providers.codex_responses_provider import (
    CodexResponsesProvider,
    _messages_to_codex_input,
    _normalize_codex_model,
    _resolve_effort,
)
from flowly.providers.factory import build_provider


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


def test_native_compaction_is_scoped_gated_and_below_local_threshold():
    prov = CodexResponsesProvider(api_key="tok", account_id="acct-1")
    state = {}

    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=100_000,
    ):
        payload, _ = prov._build_payload(
            [{"role": "user", "content": "hi"}],
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )
        old_payload, _ = prov._build_payload(
            [{"role": "user", "content": "hi"}],
            None,
            "gpt-5.5",
            "auto",
            stream=True,
        )

    assert payload["context_management"] == [{
        "type": "compaction",
        "compact_threshold": 100_000 - 8_192,
    }]
    assert "context_management" not in old_payload
    # Out-of-band calls (including the local summarizer) have no durable
    # session scope and must remain byte-compatible with the old path.
    unscoped, _ = prov._build_payload(
        [{"role": "user", "content": "hi"}],
        None,
        "gpt-5.6-sol",
        "auto",
        stream=True,
    )
    assert "context_management" not in unscoped


def test_factory_threads_native_compaction_configuration():
    config = SimpleNamespace(
        agents=SimpleNamespace(
            defaults=SimpleNamespace(llm_timeout_seconds=123),
        ),
        providers=SimpleNamespace(
            openai_codex=SimpleNamespace(
                native_compaction=False,
                native_compaction_threshold=77_000,
            ),
        ),
    )
    active = ActiveProvider(
        key="openai_codex",
        api_key="tok",
        api_base=None,
        source="test",
        account_id="acct-1",
    )

    provider = build_provider(
        active,
        default_model="gpt-5.6-sol",
        config=config,
    )

    assert isinstance(provider, CodexResponsesProvider)
    assert provider.native_compaction is False
    assert provider.native_compaction_threshold == 77_000
    assert provider.request_timeout_seconds == 123


def test_concurrent_session_scopes_do_not_share_native_state():
    prov = CodexResponsesProvider(api_key="tok", account_id="acct-1")
    disabled = {}
    prov._stamp_state(disabled, "gpt-5.6-sol")
    disabled["native_disabled"] = True
    enabled = {}

    async def build(session_key, state):
        with prov.request_scope(
            session_key=session_key,
            provider_state=state,
            local_compaction_threshold=250_000,
        ):
            await asyncio.sleep(0)
            payload, _ = prov._build_payload(
                [{"role": "user", "content": session_key}],
                None,
                "gpt-5.6-sol",
                "auto",
                stream=True,
            )
            return payload

    async def run():
        return await asyncio.gather(
            build("web:disabled", disabled),
            build("web:enabled", enabled),
        )

    disabled_payload, enabled_payload = asyncio.run(run())

    assert "context_management" not in disabled_payload
    assert "context_management" in enabled_payload


def test_headers_carry_account_id_and_beta():
    prov = CodexResponsesProvider(api_key="tok", account_id="acct-9")
    h = prov._headers("sess-1", accept="text/event-stream")
    assert h["Authorization"] == "Bearer tok"
    assert h["ChatGPT-Account-Id"] == "acct-9"
    assert h["OpenAI-Beta"] == "responses=experimental"
    assert h["session_id"] == "sess-1"


# ── SSE parsing with a fake client ─────────────────────────────────────


class _FakeStream:
    def __init__(self, status_code, lines, body=b""):
        self.status_code = status_code
        self._lines = lines
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return self._body


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

_COMPACTION_FRAMES = [
    'data: {"type":"response.output_item.done","item":{"type":"compaction",'
    '"encrypted_content":"opaque-checkpoint"}}',
    'data: {"type":"response.output_item.done","item":{"type":"message",'
    '"role":"assistant","content":[{"type":"output_text","text":"Continued"}]}}',
    'data: {"type":"response.completed","response":{"status":"completed",'
    '"output":[],"usage":{"input_tokens":20,"output_tokens":2,"total_tokens":22}}}',
    "",
]

_REASONING_FRAMES = [
    'data: {"type":"response.output_item.done","item":{"type":"reasoning",'
    '"id":"rs_123","encrypted_content":"opaque-reasoning",'
    '"summary":[{"type":"summary_text","text":"checked constraints"}]}}',
    'data: {"type":"response.output_item.done","item":{"type":"message",'
    '"id":"msg_123","role":"assistant","status":"completed",'
    '"phase":"final_answer","content":[{"type":"output_text","text":"Answer"}]}}',
    'data: {"type":"response.completed","response":{"status":"completed",'
    '"output":[],"usage":{"input_tokens":20,"output_tokens":3,"total_tokens":23}}}',
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


def test_reasoning_and_exact_message_items_round_trip_in_order():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    _install(_REASONING_FRAMES)

    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        response = asyncio.run(prov.chat(
            messages=[{"role": "user", "content": "question"}],
            model="gpt-5.6-sol",
        ))

    assert response.content == "Answer"
    assert response.provider_replay["continuity_id"] == state["continuity_id"]
    assert [
        item["type"] for item in response.provider_replay["items"]
    ] == ["reasoning", "message"]
    # Stateless reasoning ids are deliberately omitted; exact assistant output
    # metadata survives for cache/phase fidelity.
    assert "id" not in response.provider_replay["items"][0]
    assert response.provider_replay["items"][1]["id"] == "msg_123"
    assert response.provider_replay["items"][1]["phase"] == "final_answer"

    assistant = {
        "role": "assistant",
        "content": "Answer",
        PROVIDER_REPLAY_KEY: response.provider_replay,
    }
    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        payload, _ = prov._build_payload(
            [
                {"role": "user", "content": "question"},
                assistant,
                {"role": "user", "content": "follow-up"},
            ],
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )

    assert [item.get("type") for item in payload["input"]] == [
        None,
        "reasoning",
        "message",
        None,
    ]
    assert payload["input"][1]["encrypted_content"] == "opaque-reasoning"
    assert payload["input"][2]["id"] == "msg_123"
    assert payload["input"][2]["phase"] == "final_answer"


def test_foreign_replay_sidecar_falls_back_to_plain_assistant_message():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    old_state = {}
    prov._stamp_state(old_state, "gpt-5.6-sol")
    envelope = {
        **{
            key: old_state[key]
            for key in ("provider", "model", "issuer", "continuity_id")
        },
        "items": [{
            "type": "reasoning",
            "encrypted_content": "foreign",
            "summary": [],
        }],
    }
    prov.account_id = "acct-2"

    with prov.request_scope(
        session_key="web:one",
        provider_state=old_state,
        local_compaction_threshold=250_000,
    ):
        payload, _ = prov._build_payload(
            [{
                "role": "assistant",
                "content": "durable plain answer",
                PROVIDER_REPLAY_KEY: envelope,
            }],
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )

    assert payload["input"] == [{
        "role": "assistant",
        "content": "durable plain answer",
    }]


def test_compaction_checkpoint_is_captured_and_replayed_in_sequence():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    _install(_COMPACTION_FRAMES)

    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        response = asyncio.run(prov.chat(
            messages=[{"role": "user", "content": "long turn"}],
            model="gpt-5.6-sol",
        ))

    assert response.content == "Continued"
    assert response.provider_state["_checkpoint_emitted"] is True
    assert state["checkpoint"] == {
        "type": "compaction",
        "encrypted_content": "opaque-checkpoint",
    }

    marker = prov.continuity_marker(state, "gpt-5.6-sol")
    assert marker is not None
    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        payload, _ = prov._build_payload(
            [
                {"role": "user", "content": "covered"},
                marker,
                {"role": "assistant", "content": "Continued"},
                {"role": "user", "content": "next"},
            ],
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )

    assert [item.get("type") for item in payload["input"]] == [
        None,
        "compaction",
        None,
        None,
    ]
    assert payload["input"][1]["encrypted_content"] == "opaque-checkpoint"


def test_checkpoint_keeps_cache_key_stable_and_drops_on_issuer_change():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    prov._stamp_state(state, "gpt-5.6-sol")
    state["checkpoint"] = {
        "type": "compaction",
        "encrypted_content": "opaque-checkpoint",
    }
    marker = prov.continuity_marker(state, "gpt-5.6-sol")
    assert marker is not None
    messages = [
        {"role": "system", "content": "stable instructions"},
        {"role": "user", "content": "first request"},
        marker,
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "next request"},
    ]

    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        checkpoint_payload, checkpoint_cache_key = prov._build_payload(
            messages,
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )
        _, raw_cache_key = prov._build_payload(
            [message for message in messages if message is not marker],
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )

    assert checkpoint_cache_key == raw_cache_key
    assert any(
        item.get("type") == "compaction"
        for item in checkpoint_payload["input"]
    )

    # OAuth account rotation changes the encryption issuer. The raw transcript
    # remains in the request, while the now-ineligible opaque item is removed.
    prov.account_id = "acct-2"
    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        rotated_payload, _ = prov._build_payload(
            messages,
            None,
            "gpt-5.6-sol",
            "auto",
            stream=True,
        )

    assert not any(
        item.get("type") == "compaction"
        for item in rotated_payload["input"]
    )
    assert [
        item.get("content")
        for item in rotated_payload["input"]
        if item.get("role") in {"user", "assistant"}
    ] == ["first request", "first answer", "next request"]


def test_checkpoint_coverage_survives_later_non_checkpoint_response():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    prov._stamp_state(state, "gpt-5.6-sol")
    state.update({
        "checkpoint": {
            "type": "compaction",
            "encrypted_content": "opaque-checkpoint",
        },
        "_covered_turn_message_count": 2,
    })

    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        response = prov._merge_response_state(
            codex.LLMResponse(content="final"),
            "gpt-5.6-sol",
        )

    assert response.provider_state["_covered_turn_message_count"] == 2
    assert response.provider_state["checkpoint"]["encrypted_content"] == (
        "opaque-checkpoint"
    )


def test_native_field_rejection_retries_once_and_disables_only_that_session():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    captures = []
    replies = [
        _FakeStream(400, [], b"Unknown parameter: context_management"),
        _FakeStream(200, _TEXT_FRAMES),
    ]

    class SequenceClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            captures.append(kwargs["json"])
            return replies.pop(0)

    codex.httpx.AsyncClient = lambda **kwargs: SequenceClient()  # type: ignore
    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        response = asyncio.run(prov.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.6-sol",
        ))

    assert response.content == "Hello"
    assert "context_management" in captures[0]
    assert "context_management" not in captures[1]
    assert state["native_disabled"] is True


def test_bad_encrypted_checkpoint_retries_with_complete_raw_transcript():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    prov._stamp_state(state, "gpt-5.6-sol")
    state["checkpoint"] = {
        "type": "compaction",
        "encrypted_content": "bad-checkpoint",
    }
    marker = prov.continuity_marker(state, "gpt-5.6-sol")
    assert marker is not None
    captures = []
    replies = [
        _FakeStream(400, [], b'{"error":{"code":"invalid_encrypted_content"}}'),
        _FakeStream(200, _TEXT_FRAMES),
    ]

    class SequenceClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            captures.append(kwargs["json"])
            return replies.pop(0)

    codex.httpx.AsyncClient = lambda **kwargs: SequenceClient()  # type: ignore
    messages = [
        {"role": "user", "content": "old raw turn"},
        marker,
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new raw turn"},
    ]
    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        response = asyncio.run(prov.chat(
            messages=messages,
            model="gpt-5.6-sol",
        ))

    assert response.content == "Hello"
    assert any(item.get("type") == "compaction" for item in captures[0]["input"])
    assert not any(item.get("type") == "compaction" for item in captures[1]["input"])
    assert [item.get("content") for item in captures[1]["input"]] == [
        "old raw turn",
        "old answer",
        "new raw turn",
    ]
    assert "checkpoint" not in state


def test_bad_reasoning_replay_disables_opaque_items_and_retries_plain_text():
    prov = CodexResponsesProvider(
        api_key="k",
        account_id="acct-1",
        default_model="gpt-5.6-sol",
    )
    state = {}
    prov._stamp_state(state, "gpt-5.6-sol")
    envelope = {
        **{
            key: state[key]
            for key in ("provider", "model", "issuer", "continuity_id")
        },
        "items": [{
            "type": "reasoning",
            "encrypted_content": "bad-reasoning",
            "summary": [],
        }],
    }
    captures = []
    replies = [
        _FakeStream(400, [], b'{"error":{"code":"invalid_encrypted_content"}}'),
        _FakeStream(200, _TEXT_FRAMES),
    ]

    class SequenceClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            captures.append(kwargs["json"])
            return replies.pop(0)

    codex.httpx.AsyncClient = lambda **kwargs: SequenceClient()  # type: ignore
    messages = [
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": "old answer",
            PROVIDER_REPLAY_KEY: envelope,
        },
        {"role": "user", "content": "new question"},
    ]
    with prov.request_scope(
        session_key="web:one",
        provider_state=state,
        local_compaction_threshold=250_000,
    ):
        response = asyncio.run(prov.chat(
            messages=messages,
            model="gpt-5.6-sol",
        ))

    assert response.content == "Hello"
    assert any(item.get("type") == "reasoning" for item in captures[0]["input"])
    assert not any(item.get("type") == "reasoning" for item in captures[1]["input"])
    assert [item.get("content") for item in captures[1]["input"]] == [
        "old question",
        "old answer",
        "new question",
    ]
    assert state["replay_disabled"] is True
    assert state["native_disabled"] is True


def test_http_error_returns_error_response_not_raise():
    prov = _provider()
    _install(['data: {"type":"noise"}', ""], status=400)
    resp = asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))
    assert resp.finish_reason == "error"
    assert "Error calling LLM" in (resp.content or "")
    assert resp.error_info is not None
    assert resp.error_info.status_code == 400


def test_http_input_cap_preserves_structured_status():
    prov = _provider()
    _install(['data: {"type":"noise"}', ""], status=413)
    resp = asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))

    assert resp.finish_reason == "error"
    assert resp.error_info is not None
    assert resp.error_info.status_code == 413


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
