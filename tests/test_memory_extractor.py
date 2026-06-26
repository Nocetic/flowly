"""Tests for the live memory dreamer extractor (flowly/memory/extractor.py).

Covers the deterministic JSON parsing / coercion, provenance, and the real
sync→async bridge path (extract() in a worker thread, LLM call on the loop).
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.memory.dreamer import MessageRow
from flowly.memory.extractor import (
    SubagentExtractor,
    _extract_json_array,
    _provenance,
    _to_candidate,
)


# ── _extract_json_array ──────────────────────────────────────────────────────

def test_parse_plain_array():
    out = _extract_json_array('[{"text":"a"},{"text":"b"}]')
    assert [o["text"] for o in out] == ["a", "b"]


def test_parse_json_fence():
    raw = '```json\n[{"text":"x"}]\n```'
    assert _extract_json_array(raw) == [{"text": "x"}]


def test_parse_prose_wrapped():
    raw = 'Sure! Here are the memories:\n[{"text":"y"}]\nLet me know.'
    assert _extract_json_array(raw) == [{"text": "y"}]


def test_parse_empty_and_garbage():
    assert _extract_json_array("") == []
    assert _extract_json_array("no json here") == []
    assert _extract_json_array("{not an array}") == []
    assert _extract_json_array("[]") == []
    # non-dict elements are dropped
    assert _extract_json_array('["a", {"text":"ok"}, 3]') == [{"text": "ok"}]


# ── _to_candidate ────────────────────────────────────────────────────────────

def test_to_candidate_valid():
    c = _to_candidate(
        {"kind": "preference", "text": "Likes pytest", "normalized_key": "Pref:Editor",
         "privacy_level": "normal", "is_explicit": True, "confidence": 0.9},
        "chan:1", ["5"],
    )
    assert c is not None
    assert c.kind == "preference"
    assert c.text == "Likes pytest"
    assert c.normalized_key == "pref:editor"   # lowercased
    assert c.ref_kind == "inline"
    assert c.is_explicit is True
    assert c.confidence == 0.9
    assert c.source_session == "chan:1"
    assert c.source_message_ids == ["5"]


def test_to_candidate_drops_empty_text():
    assert _to_candidate({"kind": "profile", "text": "   "}, "s", []) is None
    assert _to_candidate({"kind": "profile"}, "s", []) is None


def test_to_candidate_coerces_bad_fields():
    c = _to_candidate(
        {"kind": "nonsense", "text": "t", "privacy_level": "weird", "confidence": "5"},
        "s", [],
    )
    assert c.kind == "preference"        # invalid kind → fallback
    assert c.privacy_level == "normal"   # invalid privacy → normal
    assert c.confidence == 1.0           # "5" parsed then clamped to [0,1]
    assert c.is_explicit is False        # missing → False


def test_to_candidate_fact_kind_maps_to_inline():
    # 'fact' is the KG-backed kind; the dreamer produces inline candidates.
    c = _to_candidate({"kind": "fact", "text": "t"}, "s", [])
    assert c.kind == "preference"
    assert c.ref_kind == "inline"


# ── _provenance ──────────────────────────────────────────────────────────────

def test_provenance_most_common_session_and_id_cap():
    delta = (
        [MessageRow(id=i, session_key="a", role="user", content="x", timestamp=0.0) for i in range(1, 4)]
        + [MessageRow(id=99, session_key="b", role="user", content="x", timestamp=0.0)]
    )
    session, ids = _provenance(delta)
    assert session == "a"           # 3 vs 1
    assert ids == ["1", "2", "3", "99"]
    # > 20 ids → collapse to [min, max]
    big = [MessageRow(id=i, session_key="a", role="user", content="x", timestamp=0.0) for i in range(50)]
    _, ids2 = _provenance(big)
    assert ids2 == ["0", "49"]


# ── full extract() through the real worker-thread → loop bridge ──────────────

class _Delta:
    def __init__(self, content):
        self.content = content


class _FakeProvider:
    def __init__(self, chunks):
        self._chunks = chunks

    async def chat_stream(self, messages, **kwargs):
        for c in self._chunks:
            yield _Delta(c)


def test_extract_end_to_end_bridge():
    provider = _FakeProvider(
        ['[{"kind":"preference","text":"Uses ', 'ruff","normalized_key":"pref:lint",',
         '"confidence":0.85,"is_explicit":true}]']
    )

    async def main():
        loop = asyncio.get_running_loop()
        ex = SubagentExtractor(provider=provider, model="m", loop=loop)
        delta = [MessageRow(id=7, session_key="cli:default", role="user",
                            content="I always run ruff", timestamp=0.0)]
        # extract() is sync and bridges back to this loop — exactly as the
        # dreamer calls it from a worker thread.
        return await asyncio.to_thread(ex.extract, delta)

    cands = asyncio.run(main())
    assert len(cands) == 1
    assert cands[0].kind == "preference"
    assert cands[0].text == "Uses ruff"
    assert cands[0].confidence == 0.85
    assert cands[0].source_session == "cli:default"
    assert cands[0].source_message_ids == ["7"]


def test_seconds_until_daily_logic():
    import datetime as dt

    from flowly.agent.loop import AgentLoop
    f = AgentLoop._seconds_until_daily
    now = dt.datetime(2026, 1, 1, 10, 0, 0)
    assert f("03:30", now=now) == (dt.datetime(2026, 1, 2, 3, 30) - now).total_seconds()
    assert f("23:00", now=now) == (dt.datetime(2026, 1, 1, 23, 0) - now).total_seconds()
    assert f("nope", now=now) is None
    assert f("25:00", now=now) is None


def test_dreamer_with_live_extractor_commits(tmp_path):
    """The whole Phase-1 pipeline: a session delta → the live extractor → the
    real dreamer engine → governed items. Explicit high-confidence auto-activates;
    an inferred one lands in the review queue (the UI's review queue finally has
    a source)."""
    from flowly.memory.dreamer import MemoryDreamerService, MessageRow
    from flowly.memory.governance import (
        STATUS_ACTIVE,
        STATUS_NEEDS_REVIEW,
        GovernanceStore,
    )

    gov = GovernanceStore(str(tmp_path / "gov.sqlite3"))

    class _DS:
        def __init__(self):
            self.calls = 0

        def read_since(self, watermark, limit):
            self.calls += 1
            if self.calls == 1:
                return [
                    MessageRow(id=1, session_key="cli:x", role="user",
                               content="My name is Hakan and I guess I like dark mode", timestamp=0.0),
                    MessageRow(id=2, session_key="cli:x", role="assistant",
                               content="Got it, Hakan.", timestamp=1.0),
                ]
            return []

    provider = _FakeProvider([
        '[{"kind":"profile","text":"Name is Hakan","normalized_key":"profile:name",'
        '"privacy_level":"normal","is_explicit":true,"confidence":0.95},'
        '{"kind":"preference","text":"Might prefer dark mode","normalized_key":"pref:theme",'
        '"privacy_level":"normal","is_explicit":false,"confidence":0.5}]'
    ])

    async def main():
        loop = asyncio.get_running_loop()
        ex = SubagentExtractor(provider=provider, model="m", loop=loop)
        svc = MemoryDreamerService(gov, _DS(), ex, calibrate=True)  # production uses calibrate=True
        return await asyncio.to_thread(svc.run, max_messages=100)

    res = asyncio.run(main())
    assert res.ran is True
    assert res.candidates == 2

    active = gov.list_items(status=STATUS_ACTIVE)
    review = gov.list_items(status=STATUS_NEEDS_REVIEW)
    # explicit profile fact auto-activated; inferred preference parked for review.
    assert any(i.text == "Name is Hakan" and i.kind == "profile" for i in active)
    assert any(i.kind == "preference" for i in review)
    gov.close()


def test_extract_empty_delta_skips_llm():
    # No delta → no LLM call, empty result (provider would raise if called).
    class _Boom:
        async def chat_stream(self, *a, **k):
            raise AssertionError("should not be called")
            yield  # pragma: no cover

    ex = SubagentExtractor(provider=_Boom(), model="m", loop=asyncio.new_event_loop())
    assert ex.extract([]) == []
