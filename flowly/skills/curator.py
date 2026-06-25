"""Curator — propose consolidation ops over the existing skill library
(merge narrow siblings into umbrellas, demote session-specific detail, archive
stale). Auto-applied via the shared apply layer. Mirrors flowly/memory/consolidate.
"""

from __future__ import annotations

from typing import Any


def build_curate_context(skill_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Snapshot of non-archived skills for the proposer. Pinned skills are
    included but flagged do-not-touch; the deterministic `stale` flag grounds
    archive proposals so the LLM only judges merge/demote semantics."""
    skills = [r for r in skill_rows if r.get("state") != "archived"]
    return {"skills": skills}


CURATE_PROMPT = """You maintain an AI agent's SKILL library — keep it tidy and
discoverable. Given the current skills (with usage + lifecycle state), propose
consolidation. Be conservative; never touch a skill marked pinned.

Op kinds:
- merge: fold narrow sibling skills into one umbrella. {"op":"merge",
  "targets":["<sibling>",...],"draft_name":"<umbrella>","draft_content":"<umbrella
  SKILL.md with frontmatter>","rationale":"..."}
- demote: move session-specific detail out of a skill body into references.
  {"op":"demote","targets":["<name>"],"draft_content":"<trimmed SKILL.md>",
  "draft_files":{"references/x.md":"<extracted>"},"rationale":"..."}
- archive: retire a stale/unused skill. {"op":"archive","targets":["<name>"],
  "rationale":"..."} (prefer skills already flagged state=stale)

Output ONLY JSON: {"ops":[ ... ]}. Empty list if the library is already clean.
NEVER delete; archive only. Don't touch pinned skills.

CURRENT SKILLS:
{context}
"""
