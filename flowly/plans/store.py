"""Disk-backed persistence for general plans.

Unlike ``flowly.agent.planner.state`` (which writes but never reads back),
this store is a *real* resume source: it **hydrates from disk on
construction**, so a bot restart recovers every plan. Two guarantees:

- **Atomic writes.** Every save goes to a temp file then ``os.replace`` —
  a crash mid-write can never leave a half-written plan JSON that fails to
  parse on the next hydration.
- **Best-effort, never fatal.** A disk error (permissions, full disk) logs
  and continues; the in-memory copy stays authoritative so the tool flow
  never breaks on I/O.

Layout (PLAN_MODE_PLAN.md §6):

    ~/.flowly/plan-mode/<session_key>/plan_<id>.json          # full snapshot
    ~/.flowly/plan-mode/<session_key>/plan_<id>.revisions.log  # append-only audit

One active plan per session is the norm, but the store keeps every plan
(active + terminal) keyed by id so ``plan.list`` and audits work. The
"current" plan for a session is the newest non-terminal one.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from flowly.plans.models import TERMINAL_STATUSES, GeneralPlan


def safe_filename(name: str) -> str:
    """Strip path separators / shenanigans from a session key for use as a
    directory name, keeping the readable alphabet where safe so operators can
    correlate disk files with sessions."""
    if not name:
        return "_unknown"
    out = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in name)
    out = out.strip(".")[:80]
    return out or "_unknown"


class PlanStore:
    """In-memory registry of plans, write-through to disk, hydrated on start.

    Thread-safety: an ``AgentLoop`` is single-threaded per session, but the
    store is a process singleton shared with the gateway, so a lock guards the
    dict. Disk I/O happens outside the lock.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        persist: Optional[bool] = None,
        hydrate: bool = True,
    ):
        self._plans: dict[str, GeneralPlan] = {}  # plan_id → plan
        self._lock = threading.RLock()
        # Persistence: env override > param > default ON.
        env = os.environ.get("FLOWLY_PLAN_PERSIST", "").strip().lower()
        if env in {"0", "false", "no", "off"}:
            self._persist = False
        elif env in {"1", "true", "yes", "on"}:
            self._persist = True
        else:
            self._persist = persist if persist is not None else True
        self._root_override = root
        self._root: Optional[Path] = None
        if hydrate:
            self.hydrate()

    # ── directory resolution ────────────────────────────────────────────

    def _resolve_root(self) -> Optional[Path]:
        if self._root is not None:
            return self._root
        if self._root_override is not None:
            self._root = self._root_override
        else:
            try:
                from flowly.profile import get_flowly_home

                # Dedicated dir — kept separate from the browser planner's
                # ~/.flowly/plans so the two never read each other's files.
                self._root = get_flowly_home() / "plan-mode"
            except Exception as e:
                logger.warning(f"[plans.store] cannot resolve plans dir: {e}")
                self._persist = False
                return None
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[plans.store] cannot create plans dir: {e}")
            self._persist = False
            return None
        return self._root

    # ── hydration ───────────────────────────────────────────────────────

    def hydrate(self) -> int:
        """Load every plan JSON from disk into memory. Returns count loaded.

        Corrupt files are skipped (logged) so one bad file can't block
        startup. Safe to call again — it merges by id, newest wins.
        """
        root = self._resolve_root()
        if root is None or not root.exists():
            return 0
        loaded = 0
        for sess_dir in root.iterdir():
            if not sess_dir.is_dir():
                continue
            for f in sess_dir.glob("plan_*.json"):
                if f.name.endswith(".revisions.log"):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    plan = GeneralPlan.from_dict(data)
                except Exception as e:
                    logger.warning(f"[plans.store] skip corrupt plan {f}: {e}")
                    continue
                with self._lock:
                    self._plans[plan.id] = plan
                loaded += 1
        if loaded:
            logger.info(f"[plans.store] hydrated {loaded} plan(s) from disk")
        return loaded

    # ── reads ───────────────────────────────────────────────────────────

    def get(self, plan_id: str) -> Optional[GeneralPlan]:
        with self._lock:
            return self._plans.get(plan_id)

    def current_for_session(self, session_key: str) -> Optional[GeneralPlan]:
        """The newest non-terminal plan for a session (the one a client
        should show). Falls back to None if all are terminal."""
        with self._lock:
            candidates = [
                p
                for p in self._plans.values()
                if p.sessionKey == session_key and p.status not in TERMINAL_STATUSES
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.updatedAt)

    def all_for_session(self, session_key: str) -> list[GeneralPlan]:
        with self._lock:
            plans = [p for p in self._plans.values() if p.sessionKey == session_key]
        return sorted(plans, key=lambda p: p.updatedAt, reverse=True)

    def all_plans(self) -> list[GeneralPlan]:
        with self._lock:
            return list(self._plans.values())

    # ── writes ──────────────────────────────────────────────────────────

    def save(self, plan: GeneralPlan) -> None:
        with self._lock:
            self._plans[plan.id] = plan
        self._write_through(plan)

    def _write_through(self, plan: GeneralPlan) -> None:
        if not self._persist:
            return
        root = self._resolve_root()
        if root is None:
            return
        try:
            sess_dir = root / safe_filename(plan.sessionKey)
            sess_dir.mkdir(parents=True, exist_ok=True)
            target = sess_dir / f"{plan.id}.json"
            # Atomic write: temp file in the same dir, then os.replace.
            tmp = sess_dir / f".{plan.id}.json.tmp.{os.getpid()}"
            tmp.write_text(
                json.dumps(plan.to_dict(), indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, target)
            # Append-only audit line.
            with (sess_dir / f"{plan.id}.revisions.log").open(
                "a", encoding="utf-8"
            ) as log:
                log.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "revision": plan.revision,
                            "status": plan.status,
                            "progress": plan.progress_summary(),
                        },
                        default=str,
                    )
                    + "\n"
                )
        except Exception as e:
            logger.warning(f"[plans.store] write-through failed for {plan.id}: {e}")


# ── process singleton ───────────────────────────────────────────────────

_singleton: Optional[PlanStore] = None
_singleton_lock = threading.Lock()


def get_plan_store() -> PlanStore:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = PlanStore()
    return _singleton


def reset_plan_store_singleton() -> None:
    """Test hook — drop the process singleton so a fresh temp-dir store can
    be installed."""
    global _singleton
    with _singleton_lock:
        _singleton = None
