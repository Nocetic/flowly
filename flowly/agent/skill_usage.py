"""Skill usage telemetry — per-skill counters + provenance + lifecycle state.

There is no skill telemetry today. This is the lightweight sidecar the curator
and lifecycle use to decide what's stale / consolidatable. Stored as one JSON
file (`~/.flowly/skills/.usage.json`) with a process lock + atomic replace,
mirroring the discipline in flowly/board/store.py and flowly/memory/governance.py.

Kept deliberately cheap: `use_count` is the only hot counter (bumped when a skill
is actually loaded), written only when a value changes to avoid thrashing the
hot prompt-render path.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

PROV_AGENT = "agent-created"
PROV_BUNDLED = "bundled"
PROV_WORKSPACE = "workspace"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SkillUsage:
    name: str
    use_count: int = 0
    last_used_at: Optional[str] = None
    created_at: str = ""
    provenance: str = PROV_BUNDLED
    pinned: bool = False
    state: str = STATE_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_skills_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills"


class SkillUsageStore:
    """Process-safe JSON telemetry sidecar for skills."""

    def __init__(self, skills_dir: str | Path | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else _default_skills_dir()
        self.path = self.skills_dir / ".usage.json"
        self._lock = threading.RLock()

    # -- io -----------------------------------------------------------------

    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _write(self, data: dict[str, dict]) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.skills_dir), prefix=".usage.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- mutations ----------------------------------------------------------

    def _ensure(self, data: dict, name: str) -> dict:
        rec = data.get(name)
        if rec is None:
            rec = SkillUsage(name=name, created_at=_now_iso()).to_dict()
            data[name] = rec
        return rec

    def bump_use(self, name: str, *, provenance: Optional[str] = None) -> None:
        """Record that a skill was loaded/consulted. Best-effort; never raises."""
        try:
            with self._lock:
                data = self._read()
                rec = self._ensure(data, name)
                rec["use_count"] = int(rec.get("use_count", 0)) + 1
                rec["last_used_at"] = _now_iso()
                if provenance and rec.get("provenance") in (None, PROV_BUNDLED):
                    rec["provenance"] = provenance
                # Re-use reactivates a stale skill.
                if rec.get("state") == STATE_STALE:
                    rec["state"] = STATE_ACTIVE
                self._write(data)
        except Exception as exc:
            logger.debug(f"[skill-usage] bump_use({name}) failed: {exc}")

    def set_state(self, name: str, state: str) -> None:
        with self._lock:
            data = self._read()
            self._ensure(data, name)["state"] = state
            self._write(data)

    def set_pinned(self, name: str, pinned: bool) -> None:
        with self._lock:
            data = self._read()
            self._ensure(data, name)["pinned"] = bool(pinned)
            self._write(data)

    def set_provenance(self, name: str, provenance: str) -> None:
        with self._lock:
            data = self._read()
            self._ensure(data, name)["provenance"] = provenance
            self._write(data)

    def forget(self, name: str) -> None:
        with self._lock:
            data = self._read()
            if name in data:
                del data[name]
                self._write(data)

    # -- reads --------------------------------------------------------------

    def get(self, name: str) -> Optional[SkillUsage]:
        rec = self._read().get(name)
        return SkillUsage(**rec) if rec else None

    def all(self) -> list[SkillUsage]:
        return [SkillUsage(**r) for r in self._read().values()]
