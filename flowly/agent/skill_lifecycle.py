"""Deterministic skill lifecycle — age-based staling (no LLM).

Mirrors the memory dreamer's temporal staling: a skill unused for longer than
``stale_after_days`` is marked ``stale`` (de-prioritized in the prompt) — but
NEVER auto-archived (archiving is a curator op or an explicit CLI action).
Re-use reactivates a stale skill (handled in SkillUsageStore.bump_use).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from flowly.agent.skill_usage import (
    PROV_AGENT,
    STATE_ACTIVE,
    STATE_STALE,
    SkillUsageStore,
)


@dataclass
class LifecycleResult:
    checked: int = 0
    marked_stale: int = 0
    errors: list[str] = field(default_factory=list)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class SkillLifecycle:
    def __init__(
        self,
        usage: SkillUsageStore,
        *,
        stale_after_days: int = 60,
        stale_min_uses: int = 1,
        agent_only: bool = True,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.usage = usage
        self.stale_after_days = stale_after_days
        self.stale_min_uses = stale_min_uses
        # Only stale agent-created skills by default — absence of use on a bundled
        # skill doesn't mean it's irrelevant (it's curated, may be situational).
        self.agent_only = agent_only
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run(self) -> LifecycleResult:
        res = LifecycleResult()
        now = self._now()
        cutoff_days = float(self.stale_after_days)
        for rec in self.usage.all():
            res.checked += 1
            if rec.pinned or rec.state != STATE_ACTIVE:
                continue
            if self.agent_only and rec.provenance != PROV_AGENT:
                continue
            anchor = _parse(rec.last_used_at) or _parse(rec.created_at)
            if anchor is None:
                continue
            age_days = (now - anchor).total_seconds() / 86400.0
            if age_days >= cutoff_days and rec.use_count <= self.stale_min_uses:
                try:
                    self.usage.set_state(rec.name, STATE_STALE)
                    res.marked_stale += 1
                except Exception as exc:  # never let one bad row break the pass
                    res.errors.append(f"{rec.name}: {exc}")
        return res
