"""MemoryDreamerService — cross-session memory consolidation.

This is the "dreaming" engine: a background pass that reads conversation deltas
across sessions, extracts candidate memories, reconciles them against what is
already known, and commits the survivors through the governance status machine.

Distinct from per-turn self-review (``loop._maybe_spawn_review``), which is a
fast single-session capture. The dreamer is single-writer, watermarked, and
consolidates *across* sessions on idle/daily/manual triggers.

Design for testability: the engine is deterministic and dependency-injected.
The two seams that touch the live system are *protocols*:

* ``DeltaSource`` — yields new messages since a watermark (live adapter reads
  the session-index sqlite; tests use an in-memory fake).
* ``Extractor`` — turns a message delta into ``Candidate``s. The live adapter
  spawns a **tool-less** structured-output subagent (it returns data, it does
  not write memory itself — the engine owns all writes, so the extractor needs
  no message/exec/skill_manage/write_file/cron/memory tools at all). Tests use a
  ``FakeExtractor``.

Safety invariants enforced here (not delegated):
* exactly one dreamer runs at a time (advisory lock in ``memory_meta``);
* a partial run is resumable — the watermark only advances after commit;
* injection-flagged candidates are rejected, never activated;
* sensitive/secret or low-confidence candidates never auto-activate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol, Sequence

from loguru import logger

from flowly.memory.governance import (
    ACTOR_DREAMER,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    GovernanceStore,
    MemoryItem,
)

_WATERMARK_KEY = "dreamer_watermark"
_LOCK_KEY = "dreamer_lock"

# Default commit thresholds (overridable from config in the live wiring).
DEFAULT_AUTO_FLOOR = 0.80
DEFAULT_REVIEW_FLOOR = 0.55

# An advisory lock older than this (seconds) is considered stale (crashed run)
# and may be taken over.
_LOCK_STALE_SECONDS = 1800.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_user_profile(workspace) -> str:
    """Read USER.md (the user's curated profile) from a workspace dir. Returns
    '' if it's missing or unreadable. Used to give the dreamer's extractor the
    profile as dedup context so it doesn't re-propose facts already on file."""
    try:
        from pathlib import Path

        path = Path(workspace) / "USER.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Data contracts
# --------------------------------------------------------------------------


@dataclass
class MessageRow:
    id: int
    session_key: str
    role: str
    content: str
    timestamp: float


@dataclass
class Candidate:
    """A proposed memory, as produced by an Extractor. ``confidence`` is the raw
    extractor signal; when the service runs with ``calibrate=True`` it is
    recomputed from signals (see flowly/memory/calibration.py)."""
    kind: str
    text: str
    normalized_key: str = ""
    ref_kind: str = "inline"
    ref_id: Optional[str] = None
    privacy_level: str = "normal"
    confidence: float = 0.0
    source_session: str = ""
    source_message_ids: list[str] = field(default_factory=list)
    # Calibration signal: did the user state this explicitly (vs agent inferred)?
    is_explicit: bool = False


@dataclass
class DreamResult:
    ran: bool
    processed_messages: int = 0
    candidates: int = 0
    activated: int = 0
    needs_review: int = 0
    rejected: int = 0
    duplicates: int = 0
    conflicts: int = 0
    superseded: int = 0
    watermark: int = 0
    reason: str = ""


class DeltaSource(Protocol):
    def read_since(self, watermark_id: int, limit: int) -> Sequence[MessageRow]: ...


class Extractor(Protocol):
    def extract(
        self, delta: Sequence[MessageRow], known: Sequence[MemoryItem] = ()
    ) -> Sequence[Candidate]: ...


# --------------------------------------------------------------------------
# Live adapter: read message deltas from the session index sqlite (read-only)
# --------------------------------------------------------------------------


class SessionIndexDeltaSource:
    """Reads new messages from ``session_index.sqlite`` without mutating it."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def read_since(self, watermark_id: int, limit: int) -> list[MessageRow]:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            try:
                rows = conn.execute(
                    "SELECT id, session_key, role, content, timestamp FROM messages "
                    "WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (watermark_id, limit),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning(f"[dreamer] delta read failed: {exc}")
            return []
        return [
            MessageRow(id=r[0], session_key=r[1], role=r[2], content=r[3], timestamp=r[4])
            for r in rows
        ]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


def _default_injection_check(text: str) -> bool:
    """True if the text looks like a prompt-injection attempt."""
    try:
        from flowly.cron.guard import scan_context_file
        return scan_context_file(text, "memory-candidate") is not None
    except Exception:
        return False


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).lower()


class MemoryDreamerService:
    def __init__(
        self,
        gov: GovernanceStore,
        delta_source: DeltaSource,
        extractor: Extractor,
        *,
        auto_floor: float = DEFAULT_AUTO_FLOOR,
        review_floor: float = DEFAULT_REVIEW_FLOOR,
        injection_check: Callable[[str], bool] = _default_injection_check,
        on_committed: Optional[Callable[[], None]] = None,
        lock_owner: str = "dreamer",
        calibrate: bool = False,
        calibration_weights=None,
        kg_mirror=None,
        profile_fn: Optional[Callable[[], str]] = None,
    ):
        self.gov = gov
        self.delta_source = delta_source
        self.extractor = extractor
        self.auto_floor = auto_floor
        self.review_floor = review_floor
        self.injection_check = injection_check
        # Returns the user's curated profile text (USER.md). Injected into the
        # extractor as dedup context so the dreamer doesn't re-propose facts the
        # profile already records (e.g. the user's name). None → no profile.
        self.profile_fn = profile_fn
        # Called after a successful commit pass (e.g. regenerate MEMORY.md).
        self.on_committed = on_committed
        self.lock_owner = lock_owner
        # Calibration is opt-in: when on, candidate confidence is recomputed from
        # signals (explicit/repeat/recency/conflict) rather than trusting the
        # extractor's raw number.
        self.calibrate = calibrate
        self.calibration_weights = calibration_weights
        # Optional KG mirror: when a kg_triple-backed item is superseded, the
        # underlying triple is temporally closed. None → no mirroring.
        self.kg_mirror = kg_mirror

    # -- locking ------------------------------------------------------------

    def _try_acquire_lock(self) -> bool:
        existing = self.gov.get_meta(_LOCK_KEY)
        if existing:
            try:
                _owner, ts = existing.rsplit("@", 1)
                age = (_now() - datetime.fromisoformat(ts)).total_seconds()
            except (ValueError, TypeError):
                age = _LOCK_STALE_SECONDS + 1  # malformed → treat as stale
            if age < _LOCK_STALE_SECONDS:
                return False  # someone else holds a fresh lock
            logger.warning(f"[dreamer] taking over stale lock (age={age:.0f}s)")
        self.gov.set_meta(_LOCK_KEY, f"{self.lock_owner}@{_now().isoformat()}")
        return True

    def _release_lock(self) -> None:
        self.gov.set_meta(_LOCK_KEY, "")

    # -- watermark ----------------------------------------------------------

    def _watermark(self) -> int:
        raw = self.gov.get_meta(_WATERMARK_KEY, "0")
        try:
            return int(raw or "0")
        except (ValueError, TypeError):
            return 0

    # -- known memory (reconciliation context) ------------------------------

    def _known_memory(self, *, limit: int = 80) -> list[MemoryItem]:
        """The existing active + queued memory the extractor reconciles against,
        so it dedups against what's already known and reuses an existing key when
        the conversation corrects a stored fact (which lets _resolve_conflict
        supersede the old item instead of leaving a contradicting duplicate).

        Most-recently-updated first, capped — a forensic/grounding aid, not
        load-bearing; on any error we degrade to "no context" (the prior behavior)
        rather than failing the run."""
        try:
            items = self.gov.list_items(status=STATUS_ACTIVE) + self.gov.list_items(
                status=STATUS_NEEDS_REVIEW
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dreamer] known-memory snapshot failed: {exc}")
            return []
        items.sort(key=lambda i: i.updated_at or "", reverse=True)
        return items[:limit]

    def _read_profile(self) -> str:
        """The user's curated profile (USER.md) as extra dedup context. Degrades
        to empty on any error rather than failing the run."""
        if self.profile_fn is None:
            return ""
        try:
            return self.profile_fn() or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dreamer] profile read failed: {exc}")
            return ""

    # -- main pass ----------------------------------------------------------

    def run(self, *, max_messages: int = 500) -> DreamResult:
        """Run one consolidation pass. Idempotent and crash-resumable: the
        watermark only advances after a successful commit pass."""
        if not self._try_acquire_lock():
            return DreamResult(ran=False, reason="locked")
        try:
            return self._run_inner(max_messages=max_messages)
        finally:
            self._release_lock()

    def _run_inner(self, *, max_messages: int) -> DreamResult:
        watermark = self._watermark()
        delta = list(self.delta_source.read_since(watermark, max_messages))
        if not delta:
            return DreamResult(ran=True, reason="no_delta", watermark=watermark)

        candidates = list(
            self.extractor.extract(
                delta, known=self._known_memory(), profile=self._read_profile()
            )
        )
        res = DreamResult(
            ran=True,
            processed_messages=len(delta),
            candidates=len(candidates),
            watermark=watermark,
        )

        for cand in candidates:
            self._commit_candidate(cand, res)

        # Advance watermark to the max message id we processed.
        new_watermark = max(m.id for m in delta)
        self.gov.set_meta(_WATERMARK_KEY, str(new_watermark))
        res.watermark = new_watermark

        if self.on_committed is not None:
            try:
                self.on_committed()
            except Exception as exc:  # never let summary regen break a run
                logger.warning(f"[dreamer] on_committed hook failed: {exc}")

        logger.info(
            f"[dreamer] processed={res.processed_messages} cand={res.candidates} "
            f"active={res.activated} review={res.needs_review} rej={res.rejected} "
            f"dup={res.duplicates} conflict={res.conflicts} super={res.superseded} "
            f"wm={res.watermark}"
        )
        return res

    def _commit_candidate(self, cand: Candidate, res: DreamResult) -> None:
        # 1. Injection scan — reject outright, with an audit trail.
        if self.injection_check(cand.text):
            item = self.gov.add_item(
                kind=cand.kind, text=cand.text, status=STATUS_CANDIDATE,
                ref_kind=cand.ref_kind, ref_id=cand.ref_id,
                normalized_key=cand.normalized_key, confidence=cand.confidence,
                privacy_level=cand.privacy_level, source_session=cand.source_session,
                source_message_ids=cand.source_message_ids,
                actor=ACTOR_DREAMER, reason="extracted",
            )
            self.gov.transition(
                item.id, STATUS_REJECTED, actor=ACTOR_DREAMER,
                reason="prompt_injection_flagged",
            )
            res.rejected += 1
            return

        # 2. Reconcile against same-key items.
        same_key = self.gov.find_by_key(
            cand.normalized_key,
            statuses={STATUS_ACTIVE, STATUS_NEEDS_REVIEW, STATUS_CANDIDATE},
        ) if cand.normalized_key else []

        cand_norm = _normalize_text(cand.text)
        duplicate = next(
            (i for i in same_key if _normalize_text(i.text) == cand_norm), None
        )
        if duplicate is not None:
            # Same fact seen again — repetition signal. Bump confidence + recency,
            # don't create a new row.
            self.gov.touch_seen(duplicate.id)
            bumped = min(1.0, duplicate.confidence + 0.05)
            if bumped != duplicate.confidence:
                self.gov.update_fields(duplicate.id, confidence=bumped)
            res.duplicates += 1
            return

        # Same key, different value, with a live active item → contradiction.
        active_conflict = next(
            (i for i in same_key if i.status == STATUS_ACTIVE), None
        )

        # Calibrate confidence from signals (opt-in). Replaces the raw extractor
        # number with explicit/repeat/recency/conflict-aware score.
        if self.calibrate:
            from flowly.memory.calibration import calibrate as _calibrate
            kw = {"weights": self.calibration_weights} if self.calibration_weights else {}
            # Intrinsic confidence (had_conflict=False): a contradiction is handled
            # by routing/arbitration below, not by penalizing the score — applying
            # both would double-count and stop a valid explicit update from ever
            # superseding an older fact.
            cand.confidence = _calibrate(
                is_explicit=cand.is_explicit,
                seen_count=len(same_key) + 1,
                age_days=0.0,
                had_conflict=False,
                temporal=(cand.kind == "temporal"),
                **kw,
            )

        # Contradiction with a live fact → arbitrate (may auto-supersede).
        if active_conflict is not None:
            res.conflicts += 1
            self._resolve_conflict(cand, active_conflict, res)
            return

        # No conflict: create + commit by policy.
        item = self._create_item(cand)
        target, reason = self._decide(cand, conflict=False)
        self.gov.transition(item.id, target, actor=ACTOR_DREAMER, reason=reason)
        if target == STATUS_ACTIVE:
            res.activated += 1
        elif target == STATUS_NEEDS_REVIEW:
            res.needs_review += 1
        else:
            res.rejected += 1

    def _create_item(self, cand: Candidate) -> MemoryItem:
        return self.gov.add_item(
            kind=cand.kind, text=cand.text, status=STATUS_CANDIDATE,
            ref_kind=cand.ref_kind, ref_id=cand.ref_id,
            normalized_key=cand.normalized_key, confidence=cand.confidence,
            privacy_level=cand.privacy_level, source_session=cand.source_session,
            source_message_ids=cand.source_message_ids,
            actor=ACTOR_DREAMER, reason="extracted",
        )

    def _resolve_conflict(
        self, cand: Candidate, loser: MemoryItem, res: DreamResult
    ) -> None:
        """Arbitrate a contradiction against a live active fact.

        The newcomer wins (auto-supersede) only when the user stated it
        explicitly AND it clears ``auto_floor`` — an inferred or low-confidence
        contradiction never silently overwrites a known fact; it parks in review.
        """
        wins = cand.is_explicit and cand.confidence >= self.auto_floor
        item = self._create_item(cand)
        if not wins:
            self.gov.transition(
                item.id, STATUS_NEEDS_REVIEW, actor=ACTOR_DREAMER,
                reason="contradicts_active_fact",
            )
            res.needs_review += 1
            return
        # Newcomer wins: activate it, record the supersede link, close the loser.
        self.gov.transition(
            item.id, STATUS_ACTIVE, actor=ACTOR_DREAMER,
            reason="supersedes_older_fact", supersedes=loser.id,
        )
        self.gov.transition(
            loser.id, STATUS_SUPERSEDED, actor=ACTOR_DREAMER,
            reason="superseded_by_newer",
        )
        # Mirror into the KG if the loser was a structured fact.
        if self.kg_mirror is not None and loser.ref_kind == "kg_triple" and loser.ref_id:
            self.kg_mirror.supersede(loser.ref_id)
        res.activated += 1
        res.superseded += 1

    def _decide(self, cand: Candidate, *, conflict: bool) -> tuple[str, str]:
        sensitive = cand.privacy_level in ("sensitive", "secret")
        if cand.confidence < self.review_floor:
            return STATUS_REJECTED, "below_review_floor"
        if conflict:
            return STATUS_NEEDS_REVIEW, "contradicts_active_fact"
        if sensitive:
            return STATUS_NEEDS_REVIEW, "sensitive_requires_review"
        if cand.confidence >= self.auto_floor:
            return STATUS_ACTIVE, "high_confidence_auto"
        return STATUS_NEEDS_REVIEW, "mid_confidence_review"
