"""Subagent run registry — disk-persistent, crash-safe."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

def _default_registry_path() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "subagents" / "runs.json"

_REGISTRY_PATH = None  # resolved lazily
_PRUNE_AFTER_SECONDS = 86_400  # 24 hours


@dataclass
class SubagentRunRecord:
    run_id: str
    child_session_key: str   # "subagent:{run_id}"
    parent_session_key: str  # "telegram:123456"
    parent_channel: str
    parent_chat_id: str
    task: str
    label: str               # internal/dedup key (e.g. "builtin:researcher")
    model: str | None
    cleanup: str             # "keep" | "delete"
    created_at: float
    # User-facing name, always task-derived (never a code/UUID). Falls back to
    # ``label`` only for older persisted records that predate this field.
    display_name: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    outcome: str | None = None   # "ok" | "error" | "timeout"
    error: str | None = None
    announced: bool = False
    # P1.2 — structured audit trail of every tool call the subagent made.
    # Each entry: {tool, args_bytes, result_bytes, status, duration_ms}.
    # Replaces the prior write-only list[str] that only logged names.
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


class SubagentRegistry:
    """In-memory registry with atomic disk persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_registry_path()
        self._runs: dict[str, SubagentRunRecord] = {}
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, record: SubagentRunRecord) -> None:
        self._runs[record.run_id] = record
        self._persist()
        logger.debug(f"[Registry] Registered subagent {record.run_id}: {record.label}")

    def update(self, run_id: str, **kwargs: Any) -> None:
        record = self._runs.get(run_id)
        if record is None:
            return
        for k, v in kwargs.items():
            if hasattr(record, k):
                setattr(record, k, v)
        self._persist()

    def get(self, run_id: str) -> SubagentRunRecord | None:
        return self._runs.get(run_id)

    def latest_by_label(self, label: str) -> SubagentRunRecord | None:
        """Most recently-created run carrying this label. Board cards spawn with
        ``label == card.id``, so this links a finished card to its run's
        ``tool_trace`` + timing for the task-detail/audit view."""
        if not label:
            return None
        matches = [r for r in self._runs.values() if r.label == label]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)

    def all(self) -> list[SubagentRunRecord]:
        return list(self._runs.values())

    def pending(self) -> list[SubagentRunRecord]:
        """Records that were running when process crashed (ended_at is None)."""
        return [r for r in self._runs.values() if r.ended_at is None]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Atomic write to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(r) for r in self._runs.values()]
            tmp = self._path.with_suffix(f".tmp.{secrets.token_hex(4)}")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self._path))
        except Exception as e:
            logger.warning(f"[Registry] Persist failed (non-fatal): {e}")

    def _load_from_disk(self) -> None:
        """Load persisted runs on startup. Skip stale announced records."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            cutoff = time.time() - _PRUNE_AFTER_SECONDS
            loaded = 0
            for item in raw:
                # Prune old announced records
                if item.get("announced") and (item.get("created_at", 0) < cutoff):
                    continue
                # Reconstruct record (ignore unknown fields for forward compat)
                known = {k: item[k] for k in SubagentRunRecord.__dataclass_fields__ if k in item}
                record = SubagentRunRecord(**known)
                self._runs[record.run_id] = record
                loaded += 1
            if loaded:
                logger.info(f"[Registry] Loaded {loaded} run(s) from disk")
        except Exception as e:
            logger.warning(f"[Registry] Load failed (non-fatal): {e}")
