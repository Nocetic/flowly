"""Fail-closed dreamer behaviour:
- an extractor infra failure holds the watermark (delta retried, not skipped);
- a genuine empty extraction still advances the watermark (no reprocessing);
- an injection-scanner ERROR routes the candidate to review (never silently
  active, never silently dropped), while a genuine flag still rejects.
"""

from __future__ import annotations

from flowly.memory.dreamer import (
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    Candidate,
    ExtractionError,
    MemoryDreamerService,
    MessageRow,
)
from flowly.memory.governance import GovernanceStore


def _rows(n=2):
    return [
        MessageRow(id=i, session_key="cli:a", role="user", content=f"m{i}", timestamp=float(i))
        for i in range(1, n + 1)
    ]


class _StaticDelta:
    def __init__(self, rows):
        self._rows = rows

    def read_since(self, watermark_id, limit):
        return [r for r in self._rows if r.id > watermark_id][:limit]


class _RaisingExtractor:
    def extract(self, delta, known=(), profile=""):
        raise ExtractionError("bridge down")


class _EmptyExtractor:
    def extract(self, delta, known=(), profile=""):
        return []  # LLM succeeded, genuinely nothing to learn


class _OneCandidateExtractor:
    def extract(self, delta, known=(), profile=""):
        return [Candidate(kind="preference", text="Prefers tea", normalized_key="pref:tea",
                          confidence=0.9, is_explicit=True)]


def _svc(tmp_path, extractor, injection_check=lambda t: False):
    gov = GovernanceStore(tmp_path / "gov.sqlite3")
    return gov, MemoryDreamerService(
        gov, _StaticDelta(_rows()), extractor,
        injection_check=injection_check,
    )


def test_extractor_failure_holds_watermark(tmp_path):
    gov, svc = _svc(tmp_path, _RaisingExtractor())
    res = svc.run()
    assert res.reason == "extract_failed"
    assert res.watermark == 0, "watermark must NOT advance on infra failure"
    assert gov.get_meta("dreamer_watermark", "0") in ("0", "", None)


def test_genuine_empty_advances_watermark(tmp_path):
    gov, svc = _svc(tmp_path, _EmptyExtractor())
    res = svc.run()
    assert res.candidates == 0
    assert res.watermark == 2, "a real empty extraction advances past the delta"


def test_injection_scanner_error_routes_to_review(tmp_path):
    def _boom(_text):
        raise RuntimeError("scanner unavailable")

    gov, svc = _svc(tmp_path, _OneCandidateExtractor(), injection_check=_boom)
    res = svc.run()
    assert res.needs_review == 1 and res.rejected == 0
    assert any(i.status == STATUS_NEEDS_REVIEW for i in gov.list_items())
    # watermark still advances — the candidate was handled (parked), not failed.
    assert res.watermark == 2


def test_genuine_injection_still_rejects(tmp_path):
    gov, svc = _svc(tmp_path, _OneCandidateExtractor(), injection_check=lambda t: True)
    res = svc.run()
    assert res.rejected == 1 and res.needs_review == 0
    assert any(i.status == STATUS_REJECTED for i in gov.list_items())
