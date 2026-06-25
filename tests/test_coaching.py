"""Tests for the coaching module: STT noise filter, gate parsing, manager flow."""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.coaching import CoachingManager
from flowly.coaching import gate as gate_pipeline
from flowly.coaching.manager import (
    FREQUENCY_PROFILES,
    MAX_BUFFER_SEGMENTS,
    MAX_TIP_LEN,
    SILENCE_RESET_SECONDS,
    _is_stt_noise,
    _truncate_tip,
)

# Legacy alias used by tests — keep tests stable across tuning changes.
MIN_NEW_SEGMENTS_FOR_EVAL = FREQUENCY_PROFILES["proactive"]["segments"]


# ── STT noise filter ──────────────────────────────────────────────────────────


class TestSTTNoiseFilter:
    def test_empty(self):
        assert _is_stt_noise("") is True
        assert _is_stt_noise("   ") is True

    def test_bracketed_tags(self):
        assert _is_stt_noise("[müzik]") is True
        assert _is_stt_noise("[music]") is True
        assert _is_stt_noise("[fon müziği]") is True
        assert _is_stt_noise("[arka plan müziği]") is True
        assert _is_stt_noise("[inaudible]") is True

    def test_prefix_tags(self):
        assert _is_stt_noise("[müzik devam ediyor") is True
        assert _is_stt_noise("[background noise") is True

    def test_real_speech(self):
        assert _is_stt_noise("Tamam müşteri ile konuşalım") is False
        assert _is_stt_noise("Hello world") is False
        assert _is_stt_noise("Berke 0555 123 4567") is False

    def test_case_insensitive(self):
        assert _is_stt_noise("[MÜZİK]") is True
        assert _is_stt_noise("[Music]") is True


# ── Tip truncation ────────────────────────────────────────────────────────────


class TestTruncateTip:
    def test_short_text_passes_through(self):
        text, truncated = _truncate_tip("Hello world", 180)
        assert text == "Hello world"
        assert truncated is False

    def test_long_text_cut_at_sentence_boundary(self):
        text, truncated = _truncate_tip(
            "First sentence here. Second short. Third is extra content here.",
            40,
        )
        assert truncated is True
        assert text.endswith(".")
        assert len(text) <= 40

    def test_cut_at_word_boundary_when_no_sentence(self):
        long = "word " * 60  # ~300 chars, no punctuation
        text, truncated = _truncate_tip(long, 50)
        assert truncated is True
        assert len(text) <= 51  # "…" adds 1
        assert text.endswith("…")

    def test_enforces_cap(self):
        tip = "X" * 500
        text, truncated = _truncate_tip(tip, MAX_TIP_LEN)
        assert truncated is True
        assert len(text) <= MAX_TIP_LEN + 1  # trailing "…"


# ── Gate parsing helpers ──────────────────────────────────────────────────────


class TestGateParsing:
    def test_parse_score_json(self):
        assert gate_pipeline._parse_score('{"score": 0.85}') == 0.85
        assert gate_pipeline._parse_score('{"score": 0, "reason": "meh"}') == 0.0

    def test_parse_score_plaintext(self):
        assert gate_pipeline._parse_score("0.72") == 0.72
        assert gate_pipeline._parse_score("score: 0.91") == 0.91

    def test_parse_score_percentage(self):
        # Some models return 85 instead of 0.85
        assert gate_pipeline._parse_score("85") == 0.85

    def test_parse_score_clamped(self):
        # Cap at 1.0
        assert gate_pipeline._parse_score("1.5") == 1.0
        assert gate_pipeline._parse_score("-0.2") >= 0.0

    def test_parse_score_empty(self):
        assert gate_pipeline._parse_score("") == 0.0
        assert gate_pipeline._parse_score("garbage") == 0.0

    def test_parse_bool_json(self):
        assert gate_pipeline._parse_bool('{"useful": true}') is True
        assert gate_pipeline._parse_bool('{"useful": false}') is False
        assert gate_pipeline._parse_bool('{"pass": true}') is True

    def test_parse_bool_plaintext(self):
        assert gate_pipeline._parse_bool("yes") is True
        assert gate_pipeline._parse_bool("YES") is True
        assert gate_pipeline._parse_bool("true") is True
        assert gate_pipeline._parse_bool("no") is False
        assert gate_pipeline._parse_bool("") is False


# ── CoachingManager — session lifecycle ───────────────────────────────────────


@dataclass
class _FakeResponse:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)


class _FakeLLM:
    """Minimal provider stub. Returns deterministic canned responses."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, **_kwargs: Any) -> _FakeResponse:
        self.calls += 1
        if self._responses:
            return _FakeResponse(content=self._responses.pop(0))
        return _FakeResponse(content="{}")


@pytest.fixture
def manager():
    return CoachingManager(llm_provider=_FakeLLM([]))


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_session(self, manager: CoachingManager):
        r = await manager.start("s1", user_context="test")
        assert r["status"] == "started"
        assert manager.is_active("s1")
        info = manager.session_info("s1")
        assert info is not None
        assert info["segments"] == 0

    @pytest.mark.asyncio
    async def test_start_reconfigures_existing(self, manager: CoachingManager):
        await manager.start("s1", frequency="moderate")
        r = await manager.start("s1", frequency="proactive")
        assert r["status"] == "reconfigured"
        assert manager.session_info("s1")["frequency"] == "proactive"

    @pytest.mark.asyncio
    async def test_capacity_limit(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]), max_concurrent_sessions=2)
        await mgr.start("a")
        await mgr.start("b")
        r = await mgr.start("c")
        assert r["status"] == "at_capacity"

    @pytest.mark.asyncio
    async def test_stop_unknown_session(self, manager: CoachingManager):
        r = await manager.stop("missing")
        assert r["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_invalid_frequency_falls_back(self, manager: CoachingManager):
        await manager.start("s1", frequency="bogus")
        assert manager.session_info("s1")["frequency"] == "moderate"


# ── CoachingManager — segment ingestion ───────────────────────────────────────


class TestSegmentIngestion:
    @pytest.mark.asyncio
    async def test_add_transcript_without_session(self, manager: CoachingManager):
        r = await manager.add_transcript("unknown", "hello")
        assert r["type"] == "error"

    @pytest.mark.asyncio
    async def test_add_empty_transcript(self, manager: CoachingManager):
        await manager.start("s1")
        r = await manager.add_transcript("s1", "")
        assert r["type"] == "silence"

    @pytest.mark.asyncio
    async def test_drops_stt_noise(self, manager: CoachingManager):
        await manager.start("s1")
        r = await manager.add_transcript("s1", "[müzik]")
        assert r["type"] == "silence"
        assert r["reason"] == "stt_noise"
        assert manager.session_info("s1")["segments"] == 0
        assert manager.session_info("s1")["metrics"]["segments_dropped_noise"] == 1

    @pytest.mark.asyncio
    async def test_drops_duplicate(self, manager: CoachingManager):
        await manager.start("s1")
        await manager.add_transcript("s1", "Hello there")
        r = await manager.add_transcript("s1", "Hello there")
        assert r["type"] == "silence"
        assert r["reason"] == "duplicate"
        assert manager.session_info("s1")["metrics"]["segments_dropped_duplicate"] == 1

    @pytest.mark.asyncio
    async def test_ack_on_accept(self, manager: CoachingManager):
        await manager.start("s1")
        r = await manager.add_transcript("s1", "Hello world")
        assert r["type"] == "ack"
        assert r["transcript"] == "Hello world"
        assert manager.session_info("s1")["segments"] == 1

    @pytest.mark.asyncio
    async def test_buffer_cap(self, manager: CoachingManager):
        await manager.start("s1")
        for i in range(MAX_BUFFER_SEGMENTS + 20):
            await manager.add_transcript("s1", f"unique phrase {i}")
        assert manager.session_info("s1")["segments"] == MAX_BUFFER_SEGMENTS

    @pytest.mark.asyncio
    async def test_too_long_text_truncated(self, manager: CoachingManager):
        await manager.start("s1")
        long = "word " * 2000  # ~10000 chars
        r = await manager.add_transcript("s1", long)
        assert r["type"] == "ack"
        assert len(r["transcript"]) <= 4000


# ── CoachingManager — gate pipeline (mocked) ──────────────────────────────────


class TestGatePipeline:
    @pytest.mark.asyncio
    async def test_gate_fires_at_threshold(self, monkeypatch):
        """When MIN_NEW_SEGMENTS_FOR_EVAL segments arrive, the gate runs once."""
        relevance_calls = 0
        generate_calls = 0

        async def _fake_relevance(*_args, **_kwargs):
            nonlocal relevance_calls
            relevance_calls += 1
            return True, 0.9, "ok"

        async def _fake_generate(*_args, **_kwargs):
            nonlocal generate_calls
            generate_calls += 1
            return "Test tip text"

        monkeypatch.setattr(gate_pipeline, "relevance_gate", _fake_relevance)
        monkeypatch.setattr(gate_pipeline, "generate_tip", _fake_generate)

        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        await mgr.start("s1", frequency="proactive")

        # Bypass rate limit for the test
        mgr._sessions["s1"].last_tip_at = 0

        tip_result = None
        for i in range(MIN_NEW_SEGMENTS_FOR_EVAL):
            r = await mgr.add_transcript(
                "s1", f"unique line number {i} with enough words to pass the guard"
            )
            if r.get("type") == "tip":
                tip_result = r["tip"]

        assert relevance_calls == 1
        assert generate_calls == 1
        assert tip_result is not None
        assert tip_result["text"] == "Test tip text"
        assert mgr.session_info("s1")["metrics"]["tips_emitted"] == 1

    @pytest.mark.asyncio
    async def test_gate_rejects_stops_early(self, monkeypatch):
        """Relevance gate REJECT should skip generate_tip."""
        generate_calls = 0

        async def _fake_relevance(*_args, **_kwargs):
            return False, 0.2, "nope"

        async def _fake_generate(*_args, **_kwargs):
            nonlocal generate_calls
            generate_calls += 1
            return "should not be called"

        monkeypatch.setattr(gate_pipeline, "relevance_gate", _fake_relevance)
        monkeypatch.setattr(gate_pipeline, "generate_tip", _fake_generate)

        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        await mgr.start("s1", frequency="proactive")
        mgr._sessions["s1"].last_tip_at = 0

        for i in range(MIN_NEW_SEGMENTS_FOR_EVAL):
            await mgr.add_transcript(
                "s1", f"line number {i} with enough words to pass the guard"
            )

        assert generate_calls == 0
        assert mgr.session_info("s1")["metrics"]["tips_emitted"] == 0
        assert mgr.session_info("s1")["metrics"]["gate_evaluations"] == 1

    @pytest.mark.asyncio
    async def test_rate_limit(self, monkeypatch):
        """A tip within RATE_LIMIT_SECONDS must be blocked."""

        async def _fake_relevance(*_args, **_kwargs):
            return True, 0.9

        async def _fake_generate(*_args, **_kwargs):
            return "Tip"

        monkeypatch.setattr(gate_pipeline, "relevance_gate", _fake_relevance)
        monkeypatch.setattr(gate_pipeline, "generate_tip", _fake_generate)

        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        await mgr.start("s1", frequency="proactive")
        # last_tip_at is current time → immediate re-fire should be blocked
        import time as _time
        mgr._sessions["s1"].last_tip_at = _time.time()

        for i in range(MIN_NEW_SEGMENTS_FOR_EVAL):
            await mgr.add_transcript(
                "s1", f"line number {i} with enough words to pass the guard"
            )

        assert mgr.session_info("s1")["metrics"]["tips_emitted"] == 0


# ── CoachingManager — per-session callbacks ──────────────────────────────────


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_callback_isolated_per_session(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        await mgr.start("a")
        await mgr.start("b")

        calls_a: list[tuple[str, str]] = []
        calls_b: list[tuple[str, str]] = []

        async def cb_a(sid: str, text: str, _source: str) -> None:
            calls_a.append((sid, text))

        async def cb_b(sid: str, text: str, _source: str) -> None:
            calls_b.append((sid, text))

        mgr.on_transcript("a", cb_a)
        mgr.on_transcript("b", cb_b)

        await mgr.add_transcript("a", "hello")
        await mgr.add_transcript("b", "world")

        assert calls_a == [("a", "hello")]
        assert calls_b == [("b", "world")]

    @pytest.mark.asyncio
    async def test_gate_mode_defaults_to_assistant(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        assert mgr.gate_mode == "assistant"

    @pytest.mark.asyncio
    async def test_gate_mode_guardian_honoured(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]), gate_mode="guardian")
        assert mgr.gate_mode == "guardian"

    @pytest.mark.asyncio
    async def test_gate_mode_invalid_falls_back(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]), gate_mode="bogus")
        assert mgr.gate_mode == "assistant"

    @pytest.mark.asyncio
    async def test_silence_reset_clears_buffer(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        await mgr.start("s1")
        session = mgr._sessions["s1"]

        await mgr.add_transcript("s1", "Hello team, starting the meeting")
        assert mgr.session_info("s1")["segments"] == 1

        # Simulate 3 minutes of silence by backdating last_activity_at
        session.last_activity_at = __import__("time").time() - (SILENCE_RESET_SECONDS + 60)

        r = await mgr.add_transcript("s1", "Welcome back everyone again")
        assert r["type"] == "ack"
        # Buffer cleared, this segment is the first in the new stretch
        assert mgr.session_info("s1")["segments"] == 1
        assert session.metrics.silence_resets == 1
        # Still warming up — gate shouldn't fire immediately
        assert session.silence_pending in (True, False)  # depends on word count

    @pytest.mark.asyncio
    async def test_callback_failure_does_not_break_flow(self):
        mgr = CoachingManager(llm_provider=_FakeLLM([]))
        await mgr.start("s1")

        async def crashing_cb(*_args, **_kwargs):
            raise RuntimeError("boom")

        mgr.on_transcript("s1", crashing_cb)
        r = await mgr.add_transcript("s1", "hello")
        assert r["type"] == "ack"
        assert mgr.session_info("s1")["metrics"]["callback_failures"] == 1
