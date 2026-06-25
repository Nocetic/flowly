"""Trajectory miner — detect recurring procedures across sessions and (via an
LLM proposer in the in-gateway tool) turn them into new-skill ops.

Deterministic core (detect_signals) is the cost gate: no LLM call unless a
procedure recurs across enough sessions. Mirrors flowly/memory/dreamer's
delta→watermark shape; reuses the session-index delta source.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# reuse the generic message-delta types from the memory dreamer
from flowly.memory.dreamer import MessageRow, SessionIndexDeltaSource  # noqa: F401

MINE_WATERMARK = "mine_watermark"


@dataclass
class RepeatedSignal:
    signature: str
    count: int
    sessions: list[str]
    examples: list[str]


@dataclass
class MinedSignals:
    repeated: list[RepeatedSignal] = field(default_factory=list)

    def to_context(self) -> dict[str, Any]:
        return {
            "repeated_procedures": [
                {"count": r.count, "sessions": len(r.sessions), "examples": r.examples}
                for r in self.repeated
            ]
        }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def detect_signals(
    delta: Sequence[MessageRow],
    *,
    min_evidence_sessions: int = 2,
    min_repeat_count: int = 3,
) -> Optional[MinedSignals]:
    """Find user requests that recur across ≥min_evidence_sessions distinct
    sessions with ≥min_repeat_count total occurrences. Returns None (→ skip the
    LLM entirely) when nothing clears the bar."""
    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "sessions": set(), "examples": []})
    for m in delta:
        if m.role != "user":
            continue
        sig = _norm(m.content)
        if len(sig) < 12:
            continue
        b = buckets[sig]
        b["count"] += 1
        b["sessions"].add(m.session_key)
        if len(b["examples"]) < 3:
            b["examples"].append((m.content or "")[:200])
    repeated = [
        RepeatedSignal(sig, b["count"], sorted(b["sessions"]), b["examples"])
        for sig, b in buckets.items()
        if b["count"] >= min_repeat_count and len(b["sessions"]) >= min_evidence_sessions
    ]
    repeated.sort(key=lambda r: -r.count)
    return MinedSignals(repeated) if repeated else None


MINE_PROMPT = """You design reusable SKILLS for an AI agent from recurring work.

Below are procedures the user has asked for repeatedly across multiple sessions.
For genuinely reusable, generalizable procedures, propose a NEW skill. Be
conservative — only propose a skill when it would clearly save repeated effort;
skip one-offs.

Output ONLY JSON:
{"ops":[{"op":"create","name":"<lowercase-hyphen-name>","skill_md":"<full SKILL.md
with YAML frontmatter containing a description>","rationale":"<one sentence>"}]}
Empty list if nothing is worth a skill.

RECURRING PROCEDURES:
{context}
"""
