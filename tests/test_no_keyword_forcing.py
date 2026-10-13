"""The owner's words never force a tool call, and a reply is never replaced.

A turn used to be classed as an "action" when its text matched a word list
("screenshot", "arama", "send" …). Such a turn then rejected a plain answer,
re-asked the model to call a tool twice and finally replaced the answer with
"Tool call could not be verified, no action was taken." — so "Screenshot
nedir?" got an error instead of an explanation. A failed action also armed a
lock that forced the next two messages into the same mode, and narration that
contained words like "done" or "tamamlandı" was dropped from the history.

The model now decides what a message asks for. The loop only nudges once when
a reply ends announcing a step it did not take (a trailing colon), and keeps
the model's text in every case.
"""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    """Returns the scripted responses in order and records every call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        assert self._responses, "the loop asked the model more often than scripted"
        return self._responses.pop(0)


class _ScreenshotTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return "Capture the screen."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "captured"


def _loop(tmp_path, monkeypatch, provider: LLMProvider) -> AgentLoop:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=6,
        soft_warn_at_iteration=0,
    )
    loop.tools.register(_ScreenshotTool(), toolset="test")
    return loop


async def _run(loop: AgentLoop, text: str):
    return await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": text},
        ],
        turn_content=text,
        session_key="web:forcing",
        tool_platform="web",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Screenshot nedir?",
        "arama yapma, sadece açıkla",
        "What does send mean?",
        "Telefon arama özelliği nasıl çalışıyor?",
    ],
)
async def test_an_explanation_is_answered_once_and_kept(tmp_path, monkeypatch, question):
    provider = _ScriptedProvider([LLMResponse(content="Bir açıklama.")])
    loop = _loop(tmp_path, monkeypatch, provider)
    try:
        final, _results, executed, _usage, _messages = await _run(loop, question)
    finally:
        loop.stop()

    assert final == "Bir açıklama."
    assert executed == []
    assert len(provider.calls) == 1
    assert provider.calls[0].get("tool_choice") != "required"


@pytest.mark.asyncio
async def test_a_completion_claim_without_a_tool_is_not_rewritten(tmp_path, monkeypatch):
    provider = _ScriptedProvider([LLMResponse(content="Dün raporu gönderdim, tamamlandı.")])
    loop = _loop(tmp_path, monkeypatch, provider)
    try:
        final, *_ = await _run(loop, "Dün ne yaptın?")
    finally:
        loop.stop()

    assert final == "Dün raporu gönderdim, tamamlandı."
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_narration_before_a_tool_call_stays_in_the_history(tmp_path, monkeypatch):
    provider = _ScriptedProvider([
        LLMResponse(
            content="İlk adım tamamlandı, şimdi ekranı alıyorum.",
            tool_calls=[ToolCallRequest(id="shot-1", name="screenshot", arguments={})],
        ),
        LLMResponse(content="Ekran görüntüsü hazır."),
    ])
    loop = _loop(tmp_path, monkeypatch, provider)
    try:
        final, _results, executed, _usage, messages = await _run(loop, "ekranı al")
    finally:
        loop.stop()

    assert final == "Ekran görüntüsü hazır."
    assert executed == ["screenshot"]
    narration = [
        m for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert narration and narration[0].get("content") == "İlk adım tamamlandı, şimdi ekranı alıyorum."


@pytest.mark.asyncio
async def test_an_announced_step_gets_one_nudge_then_the_reply_stands(tmp_path, monkeypatch):
    provider = _ScriptedProvider([
        LLMResponse(content="Ekrana bakıyorum:"),
        LLMResponse(content="Ekrana bakıyorum:"),
    ])
    loop = _loop(tmp_path, monkeypatch, provider)
    try:
        final, _results, executed, _usage, _messages = await _run(loop, "ekranda ne var")
    finally:
        loop.stop()

    assert len(provider.calls) == 2
    assert provider.calls[1].get("tool_choice") != "required"
    assert executed == []
    assert final == "Ekrana bakıyorum:"


@pytest.mark.asyncio
async def test_an_announced_step_that_is_then_taken_completes(tmp_path, monkeypatch):
    provider = _ScriptedProvider([
        LLMResponse(content="Ekrana bakıyorum:"),
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id="shot-2", name="screenshot", arguments={})],
        ),
        LLMResponse(content="Ekranda bir tarayıcı açık."),
    ])
    loop = _loop(tmp_path, monkeypatch, provider)
    try:
        final, _results, executed, _usage, _messages = await _run(loop, "ekranda ne var")
    finally:
        loop.stop()

    assert executed == ["screenshot"]
    assert final == "Ekranda bir tarayıcı açık."


def test_no_word_list_or_lock_decides_a_turn():
    for name in (
        "_is_action_turn",
        "_consume_pending_action_lock",
        "_set_pending_action_lock",
        "_should_promote_retry_to_action",
        "_contains_unverified_completion_claim",
    ):
        assert not hasattr(AgentLoop, name), name
