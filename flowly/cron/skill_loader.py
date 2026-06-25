"""Cron skill loading: build the [SYSTEM:]-wrapped preamble for a job.

Each requested skill is loaded via the existing SkillViewTool, and its
SKILL.md body is injected inline with a banner telling the agent the
user invoked it. Missing skills are a SOFT FAIL — the job still runs
with a `⚠️` notice so the agent can surface the gap to the user.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from flowly.agent.tools.skill_view import SkillViewTool


@dataclass
class SkillPreamble:
    """Result of assembling skill content for a cron prompt."""
    preamble: str           # Empty string when no skills requested/loaded
    loaded: list[str]       # Skill names that successfully loaded
    skipped: list[str]      # Skill names that were requested but missing


def _banner(skill_name: str) -> str:
    """The per-skill SYSTEM banner."""
    return (
        f'[SYSTEM: The user has invoked the "{skill_name}" skill, '
        "indicating they want you to follow its instructions. "
        "The full skill content is loaded below.]"
    )


async def build_skill_preamble(
    skills: list[str],
    workspace: Path,
) -> SkillPreamble:
    """Assemble a prompt preamble from the requested skill names.

    Each skill is loaded via SkillViewTool.execute(name=<name>) — the
    same mechanism agents use at runtime. If a skill is missing from
    disk, it's logged and added to `skipped[]`, and the final preamble
    is prefixed with a notice asking the agent to surface the issue.
    """
    # Filter + dedupe (matches the normalization done at add_job time,
    # but re-run here so externally-edited jobs.json values are safe too).
    normalized: list[str] = []
    for raw in skills or []:
        name = str(raw or "").strip()
        if name and name not in normalized:
            normalized.append(name)

    if not normalized:
        return SkillPreamble(preamble="", loaded=[], skipped=[])

    tool = SkillViewTool(workspace=workspace)
    parts: list[str] = []
    loaded: list[str] = []
    skipped: list[str] = []

    for name in normalized:
        try:
            raw_result = await tool.execute(name=name)
            parsed = json.loads(raw_result)
        except Exception as e:
            logger.warning(f"Cron: skill '{name}' load failed: {e}")
            skipped.append(name)
            continue

        if isinstance(parsed, dict) and parsed.get("error"):
            logger.warning(f"Cron: skill '{name}' not found — {parsed['error']}")
            skipped.append(name)
            continue

        content = str((parsed or {}).get("content") or "").strip()
        if not content:
            logger.warning(f"Cron: skill '{name}' had empty content, skipping")
            skipped.append(name)
            continue

        if parts:
            parts.append("")  # blank-line separator between skills
        parts.extend([_banner(name), "", content])
        loaded.append(name)

    # Prefix with a notice if any skill was skipped, so the agent can
    # warn the user.
    if skipped:
        names_str = ", ".join(skipped)
        notice = (
            f"[SYSTEM: The following skill(s) were listed for this job but "
            f"could not be found and were skipped: {names_str}. "
            f"Start your response with a brief notice so the user is aware, "
            f"e.g.: '⚠️ Skill(s) not found and skipped: {names_str}']"
        )
        parts = [notice, ""] + parts

    return SkillPreamble(
        preamble="\n".join(parts),
        loaded=loaded,
        skipped=skipped,
    )
