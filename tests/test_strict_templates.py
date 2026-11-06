"""Local model servers render messages through the model's own chat template.

Seen 2026-11-07 with prism-ml/bonsai-27b on LM Studio: its template raises
"System message must be at the beginning." Flowly sends its instructions and
the turn's routed guidance as two system messages, so every request failed,
the owner's messages included. Many templates (Qwen's among them) accept one
system message, first, and some also need user and assistant turns to
alternate. Requests to the local providers are shaped that way; cloud
providers keep the two system messages (prompt caching depends on it).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from flowly.providers.base import LLMResponse
from flowly.providers.openrouter_provider import OpenRouterProvider


def _strict_template(messages: list[dict[str, Any]]) -> None:
    """The checks a strict chat template makes, raising as it would."""
    for index, message in enumerate(messages):
        if message["role"] == "system" and index != 0:
            raise ValueError("System message must be at the beginning.")
    # A tool result between two assistant messages is a normal tool loop.
    roles = [m["role"] for m in messages if m["role"] != "system"]
    for before, after in zip(roles, roles[1:]):
        if before == after and before in ("user", "assistant"):
            raise ValueError("Conversation roles must alternate user/assistant/user/assistant/...")


def _provider(name: str):
    provider = OpenRouterProvider(api_key="local", api_base="http://127.0.0.1:1234/v1", provider_name=name)
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        return object()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    provider._parse_response = lambda _response: LLMResponse(content="ok")  # type: ignore[method-assign]
    return provider, sent


INSTRUCTIONS = {"role": "system", "content": "# Flowly\n\nYou are Flowly."}
GUIDANCE = {"role": "system", "content": "# Skills\n\nskill index", "_capability_guidance": []}
SUMMARY = {"role": "system", "content": "Summary of the earlier conversation."}

CONVERSATIONS = {
    "a turn": [INSTRUCTIONS, GUIDANCE, {"role": "user", "content": "selam"}],
    "after compaction": [INSTRUCTIONS, GUIDANCE, SUMMARY, {"role": "user", "content": "devam"}],
    "a note mid-conversation": [
        INSTRUCTIONS,
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "system", "content": "Thread context."},
        {"role": "user", "content": "second"},
    ],
    "with tool calls": [
        INSTRUCTIONS, GUIDANCE,
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
        {"role": "assistant", "content": "a.txt"},
        {"role": "user", "content": "thanks"},
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["vllm", "ollama"])
@pytest.mark.parametrize("conversation", list(CONVERSATIONS))
async def test_a_strict_template_accepts_every_request(name, conversation):
    provider, sent = _provider(name)
    await provider.chat(messages=[dict(m) for m in CONVERSATIONS[conversation]], model="prism-ml/bonsai-27b")
    _strict_template(sent[-1]["messages"])


@pytest.mark.asyncio
async def test_the_instructions_stay_first_so_the_cache_still_covers_them():
    provider, sent = _provider("vllm")
    await provider.chat(messages=[dict(m) for m in CONVERSATIONS["after compaction"]], model="m")
    system = sent[-1]["messages"][0]
    assert system["content"].startswith(INSTRUCTIONS["content"])
    assert "skill index" in system["content"] and "Summary of the earlier" in system["content"]
    assert [m["role"] for m in sent[-1]["messages"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_a_note_mid_conversation_is_kept_as_a_user_note():
    provider, sent = _provider("vllm")
    await provider.chat(messages=[dict(m) for m in CONVERSATIONS["a note mid-conversation"]], model="m")
    last = sent[-1]["messages"][-1]
    assert last == {"role": "user", "content": "[System note]\nThread context.\n\nsecond"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openrouter", "flowly", "openai"])
async def test_cloud_providers_keep_the_two_system_messages(name):
    provider, sent = _provider(name)
    await provider.chat(messages=[dict(m) for m in CONVERSATIONS["a turn"]], model="m")
    assert [m["role"] for m in sent[-1]["messages"]] == ["system", "system", "user"]


@pytest.mark.asyncio
async def test_a_streamed_request_is_shaped_too():
    provider, sent = _provider("vllm")
    async for _ in provider.chat_stream(messages=[dict(m) for m in CONVERSATIONS["a turn"]], model="m"):
        break
    _strict_template(sent[-1]["messages"])
