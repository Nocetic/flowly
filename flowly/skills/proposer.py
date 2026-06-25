"""Shared LLM-proposal parsing for the miner + curator.

The LLM proposes skill ops as JSON; parse_specs turns them into SkillOpSpecs
(fence-tolerant, never raises) — the deterministic apply layer validates + runs
them. Mirrors flowly/memory/consolidate.parse_operations.
"""

from __future__ import annotations

import json

from flowly.skills.apply import SkillOpSpec
from flowly.skills.op_log import VALID_KINDS


def parse_specs(raw: str) -> list[SkillOpSpec]:
    """Parse an LLM response into validated SkillOpSpecs. Best-effort."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    out: list[SkillOpSpec] = []
    for o in data.get("ops", []):
        if not isinstance(o, dict):
            continue
        kind = o.get("op") or o.get("kind")
        if kind not in VALID_KINDS:
            continue
        targets = o.get("targets") or ([o["target"]] if o.get("target") else [])
        files = o.get("draft_files") or {}
        if not isinstance(files, dict):
            files = {}
        spec = SkillOpSpec(
            kind=kind,
            targets=[str(t) for t in targets if t],
            draft_name=str(o.get("draft_name") or o.get("name") or ""),
            draft_content=str(o.get("draft_content") or o.get("skill_md") or ""),
            draft_files={str(k): str(v) for k, v in files.items()},
            rationale=str(o.get("rationale") or o.get("reason") or ""),
            evidence=o.get("evidence") if isinstance(o.get("evidence"), dict) else {},
        )
        # basic per-kind sanity (apply layer re-validates)
        if kind in ("create", "merge") and not (spec.draft_name and spec.draft_content):
            continue
        if kind in ("archive", "demote") and not spec.targets:
            continue
        out.append(spec)
    return out
