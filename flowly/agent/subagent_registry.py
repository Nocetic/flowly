"""Subagent run registry — disk-persistent, crash-safe."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock
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
    kind: str = "subagent"
    agent_id: str | None = None
    updated_at: float | None = None
    revision: int = 0
    activity: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    result_preview: str = ""
    result_chars: int = 0
    result_available: bool = False
    artifact_ids: list[str] = field(default_factory=list)
    delivery_state: str = "pending"



class SubagentRegistry:
    """Atomic, locked snapshots; result bodies live outside the list index.

    Persistence errors propagate: a successful API reply must mean the change
    survived on disk. A broken index is never silently overwritten with [] .
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_registry_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self._path) + ".lock", timeout=5)
        self._runs: dict[str, SubagentRunRecord] = {}
        try:
            self._load_from_disk()
        except (OSError, ValueError):
            # Keep the host usable. Reads/writes still raise until the index is
            # repaired; an unavailable history must never become an empty one.
            logger.exception("Subagent history is unavailable")

    @staticmethod
    def _decode(item: Any) -> SubagentRunRecord:
        if not isinstance(item, dict):
            raise ValueError("invalid run record")
        known = {k: item[k] for k in SubagentRunRecord.__dataclass_fields__ if k in item}
        record = SubagentRunRecord(**known)
        for name in ("run_id", "task", "label", "display_name", "parent_session_key",
                     "parent_channel", "parent_chat_id", "child_session_key", "kind"):
            if not isinstance(getattr(record, name), str):
                raise ValueError("invalid run text")
        if not record.run_id or record.created_at is None:
            raise ValueError("missing run identity")
        for name in ("created_at", "started_at", "ended_at", "updated_at"):
            value = getattr(record, name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("invalid run timestamp")
        for name in ("model", "outcome", "error", "error_code", "agent_id"):
            if getattr(record, name) is not None and not isinstance(getattr(record, name), str):
                raise ValueError("invalid optional run text")
        if not isinstance(record.result_preview, str) or type(record.result_chars) is not int or record.result_chars < 0:
            raise ValueError("invalid run result")
        if type(record.result_available) is not bool or type(record.announced) is not bool:
            raise ValueError("invalid run flags")
        if type(record.revision) is not int or record.revision < 0:
            raise ValueError("invalid revision")
        if not isinstance(record.activity, dict) or not isinstance(record.tool_trace, list):
            raise ValueError("invalid run activity")
        if any(not isinstance(step, dict) or not isinstance(step.get("tool", ""), str)
               for step in record.tool_trace):
            raise ValueError("invalid tool history")
        if not isinstance(record.artifact_ids, list) or any(not isinstance(x, str) for x in record.artifact_ids):
            raise ValueError("invalid artifact references")
        return record

    def _read_locked(self) -> None:
        records = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("Invalid subagent registry")
            for item in raw:
                try:
                    record = self._decode(item)
                except (TypeError, ValueError):
                    logger.warning("Skipping invalid subagent history record")
                    continue
                records[record.run_id] = record
        cutoff = time.time() - _PRUNE_AFTER_SECONDS
        records = {
            key: rec for key, rec in records.items()
            if rec.ended_at is None or rec.ended_at >= cutoff
            or not (rec.announced or rec.delivery_state == "not_required")
        }
        # Preserve references held by the live manager/board.
        for key, rec in records.items():
            if key in self._runs:
                self._runs[key].__dict__.update(rec.__dict__)
                records[key] = self._runs[key]
        self._runs = records

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp." + secrets.token_hex(8))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            if os.name == "posix":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    def _write_locked(self) -> None:
        data = json.dumps([asdict(r) for r in self._runs.values()], ensure_ascii=False)
        self._atomic_write(self._path, data.encode("utf-8"))
        # Only our own hashed files are eligible; never follow arbitrary paths.
        keep = {self._result_path(r.run_id).name for r in self._runs.values()}
        if self._result_dir.exists():
            for path in self._result_dir.glob("*.txt"):
                if re.fullmatch(r"[0-9a-f]{64}\.txt", path.name) and path.name not in keep:
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Could not prune an expired subagent result")

    @property
    def _result_dir(self) -> Path:
        return self._path.with_name(self._path.stem + "-results")

    def _result_path(self, run_id: str) -> Path:
        return self._result_dir / (hashlib.sha256(run_id.encode()).hexdigest() + ".txt")

    def register(self, record: SubagentRunRecord) -> None:
        with self._lock:
            self._read_locked()
            if record.run_id in self._runs:
                raise ValueError("Duplicate subagent run ID")
            self._decode(asdict(record))
            record.updated_at = record.created_at
            record.revision = 1
            self._runs[record.run_id] = record
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise

    def update(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None:
                return
            if record.ended_at is not None:
                changes = {k: v for k, v in changes.items() if k in ("announced", "delivery_state")}
            if not changes:
                return
            for key, value in changes.items():
                if key in record.__dataclass_fields__ and key not in ("run_id", "revision", "created_at"):
                    setattr(record, key, value)
            if record.ended_at is not None:
                for entry in record.tool_trace:
                    if entry.get("status") == "running":
                        entry.update(status=record.outcome or "interrupted", ended_at=record.ended_at)
                record.activity = {"phase": "finished"}
            record.updated_at = time.time()
            record.revision += 1
            try:
                self._decode(asdict(record))
                self._write_locked()
            except Exception:
                self._read_locked()
                raise

    def finish(self, run_id: str, outcome: str, *, result: str = "",
               error: str | None = None, error_code: str | None = None) -> None:
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None or record.ended_at is not None:
                return
            if result:
                self._atomic_write(self._result_path(run_id), result.encode("utf-8"))
            self.update(run_id, ended_at=time.time(), outcome=outcome, error=error,
                        error_code=error_code, result_preview=result[:1200],
                        result_chars=len(result), result_available=bool(result),
                        delivery_state="not_required" if outcome in ("cancelled", "interrupted") else record.delivery_state)

    def read_result(self, run_id: str, offset: int = 0, limit: int = 32000) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200000:
            raise ValueError("Invalid result range")
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None or not record.result_available:
                raise FileNotFoundError("No saved result for this run")
            # Bounded memory even for large responses; offsets count Unicode characters.
            with self._result_path(run_id).open(encoding="utf-8") as stream:
                remaining = offset
                while remaining:
                    chunk = stream.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                content = stream.read(limit)
            end = offset + len(content)
            return {"runId": run_id, "content": content, "offset": offset,
                    "totalChars": record.result_chars,
                    "nextOffset": end if end < record.result_chars else None}

    def get(self, run_id: str) -> SubagentRunRecord | None:
        return self._runs.get(run_id)

    def all(self) -> list[SubagentRunRecord]:
        self._load_from_disk()
        return list(self._runs.values())

    def latest_by_label(self, label: str) -> SubagentRunRecord | None:
        return max((r for r in self.all() if label and r.label == label),
                   key=lambda r: r.created_at, default=None)

    def pending(self) -> list[SubagentRunRecord]:
        return [r for r in self.all() if r.ended_at is None]

    def _load_from_disk(self) -> None:
        with self._lock:
            self._read_locked()
