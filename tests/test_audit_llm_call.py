"""Tests for AuditLogger.log_llm_call.

The method was defined for a long time but never called from the agent
loop. Now that the chat path wires it up after every retry-chain, the
JSONL records are the only signal we have for in-the-wild
- token usage drift (model getting more expensive on the same prompts),
- tool-loop blowups (high ``iteration`` counts before stop),
- cache hit rate (cache_read_tokens / prompt_tokens),
- per-family behaviour comparisons (gpt vs claude vs gemini).

This test pins the on-disk shape so log scrapers / dashboards built
on top don't break silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.audit.logger import AuditLogger


@pytest.fixture
def audit_dir(tmp_path) -> Path:
    d = tmp_path / "audit"
    d.mkdir()
    return d


@pytest.fixture
def logger(audit_dir) -> AuditLogger:
    return AuditLogger(audit_dir=audit_dir)


def _read_records(audit_dir: Path) -> list[dict]:
    files = sorted(audit_dir.glob("*.jsonl"))
    out: list[dict] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


class TestRequiredFields:
    def test_minimal_call_has_required_fields(self, logger, audit_dir) -> None:
        logger.log_llm_call(
            session_key="telegram:42",
            model="anthropic/claude-haiku-4.5",
            prompt_tokens=1200,
            completion_tokens=300,
            duration_ms=850,
        )
        records = _read_records(audit_dir)
        assert len(records) == 1
        r = records[0]
        assert r["type"] == "llm_call"
        assert r["session"] == "telegram:42"
        assert r["model"] == "anthropic/claude-haiku-4.5"
        assert r["prompt_tokens"] == 1200
        assert r["completion_tokens"] == 300
        assert r["total_tokens"] == 1500
        assert r["duration_ms"] == 850
        # Defaults
        assert r["tool_choice"] == "auto"
        assert r["iteration"] == 0
        # Timestamp is ISO-formatted with timezone
        assert "T" in r["ts"]
        assert r["ts"].endswith("+00:00")

    def test_total_tokens_is_computed(self, logger, audit_dir) -> None:
        logger.log_llm_call(
            session_key="cli:test",
            model="openai/gpt-5.5",
            prompt_tokens=500,
            completion_tokens=200,
            duration_ms=400,
        )
        r = _read_records(audit_dir)[0]
        assert r["total_tokens"] == 700


class TestOptionalFields:
    """Optional fields appear only when set — keeps JSONL compact and
    old log scrapers that ignore unknown fields keep working.
    """

    def test_optional_fields_omitted_when_default(
        self, logger, audit_dir,
    ) -> None:
        logger.log_llm_call(
            session_key="x:y",
            model="anthropic/claude-sonnet-4.6",
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=200,
        )
        r = _read_records(audit_dir)[0]
        for absent in (
            "finish_reason", "families",
            "cache_read_tokens", "cache_write_tokens",
            "streamed", "purpose",
        ):
            assert absent not in r, f"unexpected {absent!r} in minimal record"

    def test_finish_reason_recorded(self, logger, audit_dir) -> None:
        logger.log_llm_call(
            session_key="x:y", model="openai/gpt-5.5",
            prompt_tokens=10, completion_tokens=5, duration_ms=100,
            finish_reason="tool_calls",
        )
        assert _read_records(audit_dir)[0]["finish_reason"] == "tool_calls"

    def test_finish_reason_error_recorded(self, logger, audit_dir) -> None:
        # The error finish state is the strongest debug signal —
        # context overflow, schema rejection, key rotation cascades
        # all surface as finish_reason="error" in the chat path.
        logger.log_llm_call(
            session_key="x:y", model="openai/gpt-5.5",
            prompt_tokens=10, completion_tokens=0, duration_ms=2000,
            finish_reason="error",
        )
        assert _read_records(audit_dir)[0]["finish_reason"] == "error"

    def test_families_sorted_for_stable_ordering(
        self, logger, audit_dir,
    ) -> None:
        # detect_model_families returns a frozenset → caller may pass
        # it as a list in any order; we sort it on write so two
        # records with the same family set compare byte-equal when
        # diffed.
        logger.log_llm_call(
            session_key="x:y", model="openai/gpt-5.5",
            prompt_tokens=10, completion_tokens=5, duration_ms=100,
            families=["chinese", "openai"],
        )
        assert _read_records(audit_dir)[0]["families"] == ["chinese", "openai"]

    def test_cache_tokens_omitted_when_zero(self, logger, audit_dir) -> None:
        # Anthropic providers report 0 cache tokens on cache miss —
        # don't bloat the record with explicit zeros, the absence
        # IS the signal.
        logger.log_llm_call(
            session_key="x:y", model="anthropic/claude-sonnet-4.6",
            prompt_tokens=100, completion_tokens=50, duration_ms=300,
            cache_read_tokens=0, cache_write_tokens=0,
        )
        r = _read_records(audit_dir)[0]
        assert "cache_read_tokens" not in r
        assert "cache_write_tokens" not in r

    def test_cache_tokens_present_when_nonzero(
        self, logger, audit_dir,
    ) -> None:
        logger.log_llm_call(
            session_key="x:y", model="anthropic/claude-sonnet-4.6",
            prompt_tokens=1500, completion_tokens=200, duration_ms=400,
            cache_read_tokens=1200, cache_write_tokens=300,
        )
        r = _read_records(audit_dir)[0]
        assert r["cache_read_tokens"] == 1200
        assert r["cache_write_tokens"] == 300

    def test_streamed_flag_distinguishes_paths(
        self, logger, audit_dir,
    ) -> None:
        # Streaming vs blocking chat have different latency
        # characteristics; the flag lets aggregations split them.
        logger.log_llm_call(
            session_key="x:y", model="openai/gpt-5.5",
            prompt_tokens=10, completion_tokens=5, duration_ms=100,
            streamed=True,
        )
        logger.log_llm_call(
            session_key="x:y", model="openai/gpt-5.5",
            prompt_tokens=10, completion_tokens=5, duration_ms=100,
            streamed=False,
        )
        records = _read_records(audit_dir)
        assert records[0]["streamed"] is True
        assert records[1]["streamed"] is False

    def test_purpose_carries_through(self, logger, audit_dir) -> None:
        # The coaching path passes purpose="coaching" so the proxy
        # routes to a different rate-limit bucket; audit must
        # surface this so we can attribute costs.
        logger.log_llm_call(
            session_key="coach:42", model="openai/gpt-5.5",
            prompt_tokens=300, completion_tokens=100, duration_ms=400,
            purpose="coaching",
        )
        assert _read_records(audit_dir)[0]["purpose"] == "coaching"


class TestDailyRotation:
    """File-per-day rotation is the audit log's only retention story.
    A scraper that grouped by file name would silently mis-attribute
    if the rotation contract slipped.
    """

    def test_one_file_per_day(self, logger, audit_dir) -> None:
        for _ in range(3):
            logger.log_llm_call(
                session_key="x:y", model="m",
                prompt_tokens=1, completion_tokens=1, duration_ms=1,
            )
        files = list(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        # Filename is YYYY-MM-DD format
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        assert files[0].name == f"{today}.jsonl"

    def test_lines_are_jsonl_one_per_record(self, logger, audit_dir) -> None:
        for i in range(5):
            logger.log_llm_call(
                session_key=f"s:{i}", model="m",
                prompt_tokens=i, completion_tokens=i, duration_ms=i,
            )
        records = _read_records(audit_dir)
        assert len(records) == 5
        # All distinct
        sessions = [r["session"] for r in records]
        assert sessions == [f"s:{i}" for i in range(5)]
