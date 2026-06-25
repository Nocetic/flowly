"""Per-session plan state with optional filesystem persistence.

Phase 1: in-memory only.
Phase 2 (this commit): write-through to disk under
  ~/.flowly/plans/<session_id>/<plan_id>.json
plus an append-only revision log per plan. Manus-style external
memory — survives session compaction and gives operators a tangible
audit trail.

One plan per (session_id, tab_id) pair so an agent driving multiple
tabs in parallel doesn't trash its own state.

Thread-safety: each AgentLoop is single-threaded per session, so we
don't need real locks for correctness. The lock is paranoia for the
shared singleton.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from flowly.agent.planner.models import Plan


class PlanStateManager:
    """In-memory store keyed by (session_id, tab_id?).

    Tab-aware: a session that opens two tabs (e.g. "fill form on tab A
    while extracting data on tab B") gets two distinct plans. When
    `tab_id` is None we fall back to a session-default slot.

    GC: plans older than `max_age_seconds` are reaped on every access
    so a long-running gateway doesn't grow unbounded. 24h cap is
    generous — most browser tasks finish in minutes.
    """

    def __init__(self, max_age_seconds: int = 86_400, persist: Optional[bool] = None):
        self._plans: dict[str, Plan] = {}
        self._max_age = max_age_seconds
        self._lock = threading.Lock()  # paranoia; single-threaded in practice
        # Persistence: env override > param > default ON. Disk write
        # failures NEVER raise — they log and continue, so a permission
        # issue or full disk can't break the in-memory tool flow.
        env = os.environ.get("FLOWLY_BROWSER_PLAN_PERSIST", "").strip().lower()
        if env in {"0", "false", "no", "off"}:
            self._persist = False
        elif env in {"1", "true", "yes", "on"}:
            self._persist = True
        else:
            self._persist = persist if persist is not None else True
        # Resolve plan dir lazily so import-time can't fail on a weird env
        self._dir: Optional[Path] = None

    @staticmethod
    def _key(session_id: str, tab_id: Optional[int | str]) -> str:
        return f"{session_id}::{tab_id if tab_id is not None else '_default'}"

    def get(self, session_id: str, tab_id: Optional[int | str] = None) -> Optional[Plan]:
        with self._lock:
            self._reap_expired()
            return self._plans.get(self._key(session_id, tab_id))

    def set(self, plan: Plan, tab_id: Optional[int | str] = None) -> None:
        with self._lock:
            key = self._key(plan.sessionId, tab_id)
            self._plans[key] = plan
        # Persist outside the lock — disk I/O shouldn't block other
        # state operations. Failures are logged not raised.
        self._write_through(plan)

    def clear(self, session_id: str, tab_id: Optional[int | str] = None) -> Optional[Plan]:
        with self._lock:
            return self._plans.pop(self._key(session_id, tab_id), None)

    # ── persistence ──────────────────────────────────────────────────

    def _resolve_dir(self) -> Optional[Path]:
        """Lazy-resolve the plans dir. Returns None on failure."""
        if self._dir is not None:
            return self._dir
        try:
            from flowly.profile import get_flowly_home
            self._dir = get_flowly_home() / "plans"
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[planner.state] cannot resolve plans dir: {e} — persistence disabled")
            self._persist = False
            return None
        return self._dir

    def _write_through(self, plan: Plan) -> None:
        if not self._persist:
            return
        root = self._resolve_dir()
        if root is None:
            return
        try:
            sess_dir = root / _safe_filename(plan.sessionId)
            sess_dir.mkdir(parents=True, exist_ok=True)
            # plan.json: full state, overwritten on every mutation
            (sess_dir / f"{plan.id}.json").write_text(
                json.dumps(plan.to_dict(), indent=2, default=str),
                encoding="utf-8",
            )
            # revisions.log: append-only audit, one JSON line per change
            with (sess_dir / f"{plan.id}.revisions.log").open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "status": plan.status,
                    "progress": plan.progress_summary(),
                }, default=str) + "\n")
        except Exception as e:
            # Log but never raise — disk I/O failure must not break tool flow.
            logger.warning(f"[planner.state] write-through failed for {plan.id}: {e}")

    def all_for_session(self, session_id: str) -> list[Plan]:
        with self._lock:
            prefix = f"{session_id}::"
            return [p for k, p in self._plans.items() if k.startswith(prefix)]

    def _reap_expired(self) -> None:
        """Drop plans older than max_age. Called inside locked region."""
        now = time.time()
        cutoff = now - self._max_age
        stale_keys = [k for k, p in self._plans.items() if p.updatedAt < cutoff]
        for k in stale_keys:
            logger.info(f"[planner] reaping stale plan {self._plans[k].id} (age > {self._max_age}s)")
            del self._plans[k]


def _safe_filename(name: str) -> str:
    """Strip path separators and other shenanigans from a session id
    before using it as a directory name. Keeps the original alphabet
    where safe so operators can correlate disk files with sessions."""
    if not name:
        return "_unknown"
    safe = []
    for ch in name:
        if ch.isalnum() or ch in "-_.":
            safe.append(ch)
        else:
            safe.append("_")
    out = "".join(safe).strip(".")[:64]
    return out or "_unknown"


# Singleton — one manager per process. Created on first import so
# that both browser_plan.py and (later) loop.py's end-turn guard
# share the same state.
_singleton: Optional[PlanStateManager] = None


def get_plan_state() -> PlanStateManager:
    global _singleton
    if _singleton is None:
        _singleton = PlanStateManager()
    return _singleton
