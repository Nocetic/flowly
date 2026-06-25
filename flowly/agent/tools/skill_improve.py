"""skill_improve tool — run the trajectory miner / curator through the agent's
authenticated provider (the standalone CLI provider path 504s; stream like
memory_consolidate). LLM proposes ops; the deterministic apply layer runs them.

Registered only when skill_improvement is enabled.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.skills.curator import CURATE_PROMPT, build_curate_context
from flowly.skills.miner import MINE_PROMPT, MINE_WATERMARK, detect_signals
from flowly.skills.op_log import ACTOR_CURATOR, ACTOR_MINER
from flowly.skills.proposer import parse_specs


class SkillImproveTool(Tool):
    def __init__(self, *, facade, provider, model, delta_source, skills_loader, usage,
                 min_evidence_sessions=2, min_repeat_count=3, max_messages=1000):
        self._facade = facade
        self._provider = provider
        self._model = model
        self._delta = delta_source
        self._skills = skills_loader
        self._usage = usage
        self._min_sessions = min_evidence_sessions
        self._min_repeat = min_repeat_count
        self._max_messages = max_messages

    @property
    def name(self) -> str:
        return "skill_improve"

    @property
    def description(self) -> str:
        return (
            "Improve the skill library: mode='mine' creates new skills from "
            "recurring procedures in recent conversations; mode='curate' "
            "consolidates existing skills (merge/demote/archive). Changes are "
            "auto-applied under a snapshot (undoable). Use dry_run=true to preview."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["mine", "curate"]},
                "dry_run": {"type": "boolean"},
            },
            "required": ["mode"],
        }

    async def _stream(self, prompt: str) -> str:
        raw = ""
        for attempt in range(3):
            parts: list[str] = []
            try:
                async for d in self._provider.chat_stream(
                    [{"role": "user", "content": prompt}],
                    model=self._model, max_tokens=4096, temperature=0.1,
                ):
                    if d.content:
                        parts.append(d.content)
            except Exception as exc:
                logger.warning(f"[skill-improve] attempt {attempt+1} failed: {exc}")
                continue
            raw = "".join(parts)
            if raw.strip():
                break
        return raw

    def _skill_rows(self) -> list[dict]:
        from flowly.agent.skill_usage import PROV_AGENT
        rows = []
        try:
            for s in self._skills.list_skills(filter_unavailable=False):
                name = s.get("name")
                u = self._usage.get(name)
                # Only curate skills the AGENT created itself (apply stamps
                # provenance=agent-created). Never touch the user's installed
                # library (bundled/hub/managed-but-not-agent) — that would both
                # risk archiving skills they chose AND blow up the prompt with
                # dozens of pre-installed skills.
                if u is None or u.provenance != PROV_AGENT:
                    continue
                desc = ""
                try:
                    meta = self._skills.get_skill_metadata(name) or {}
                    desc = meta.get("description", "")
                except Exception:
                    pass
                rows.append({
                    "name": name, "description": desc,
                    "use_count": u.use_count if u else 0,
                    "last_used_at": u.last_used_at if u else None,
                    "state": u.state if u else "active",
                    "pinned": u.pinned if u else False,
                    "provenance": u.provenance if u else s.get("source", "bundled"),
                })
        except Exception as exc:
            logger.warning(f"[skill-improve] skill_rows failed: {exc}")
        return rows

    async def execute(self, mode: str = "mine", dry_run: bool = False, **kwargs: Any) -> str:
        new_wm = None
        if mode == "mine":
            wm = int(self._facade.log.get_meta(MINE_WATERMARK, "0") or 0)
            delta = list(self._delta.read_since(wm, self._max_messages))
            if not delta:
                return "Skill mine: no new conversation to mine."
            new_wm = max(m.id for m in delta)
            signals = detect_signals(
                delta, min_evidence_sessions=self._min_sessions,
                min_repeat_count=self._min_repeat,
            )
            if signals is None:
                self._facade.log.set_meta(MINE_WATERMARK, str(new_wm))
                return "Skill mine: no recurring procedures found."
            prompt = MINE_PROMPT.replace("{context}", json.dumps(signals.to_context(), ensure_ascii=False))
            actor = ACTOR_MINER
        elif mode == "curate":
            ctx = build_curate_context(self._skill_rows())
            if not ctx["skills"]:
                return "Skill curate: no skills to consolidate."
            prompt = CURATE_PROMPT.replace("{context}", json.dumps(ctx, ensure_ascii=False))
            actor = ACTOR_CURATOR
        else:
            return f"Error: unknown mode '{mode}'."

        raw = await self._stream(prompt)
        specs = parse_specs(raw)
        if not specs:
            if new_wm is not None:
                self._facade.log.set_meta(MINE_WATERMARK, str(new_wm))
            return f"Skill {mode}: no ops proposed (LLM returned {len(raw)} chars)."

        if dry_run:
            lines = [f"- {s.kind} {', '.join(s.targets) or s.draft_name}: {s.rationale}" for s in specs]
            return "Proposed (dry-run, not applied):\n" + "\n".join(lines)

        res = await self._facade.apply_specs(specs, actor=actor, reason=mode)
        if new_wm is not None:
            self._facade.log.set_meta(MINE_WATERMARK, str(new_wm))
        if mode == "curate":
            self._facade.clear_dirty()
        out = f"Skill {mode}: applied={res.applied} failed={res.failed}"
        if res.errors:
            out += " | " + "; ".join(res.errors[:3])
        return out
