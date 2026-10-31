"""memory_consolidate tool — run the consolidation pass via the agent's own
(already-authenticated) provider, instead of a separate CLI LLM call.

Registered only when memory governance is enabled. The agent calls it when the
user asks to clean up / consolidate memory. The LLM merely *proposes*; the
deterministic, tested ``apply_operations`` does the writing (audited, no deletes).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.memory.consolidate import (
    PROMPT,
    apply_operations,
    build_context,
    owner_profile,
    parse_operations,
)


class MemoryConsolidateTool(Tool):
    def __init__(self, *, facade, provider, model: str, kg_summary_fn: Callable[[], str] | None = None):
        self._facade = facade
        self._provider = provider
        self._model = model
        self._kg_summary_fn = kg_summary_fn or (lambda: "")

    @property
    def name(self) -> str:
        return "memory_consolidate"

    @property
    def description(self) -> str:
        return (
            "Consolidate long-term memory: merge duplicate facts recorded under "
            "different keys, retire free-form notes already captured in the "
            "knowledge graph, and mark outdated notes stale. Use when the user "
            "asks to clean up, tidy, or consolidate memory. Pass dry_run=true to "
            "preview the proposed changes without applying them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview proposed changes without applying.",
                }
            },
            "required": [],
        }

    async def execute(self, dry_run: bool = False, **kwargs: Any) -> str:
        gov = self._facade.gov
        kg_summary = self._kg_summary_fn()
        workspace = getattr(getattr(self._facade, "memory_store", None), "workspace", None)
        ctx = build_context(gov, kg_summary, owner_profile(workspace))
        if not ctx["items"]:
            return "Memory is empty — nothing to consolidate."

        prompt = PROMPT.replace("{context}", json.dumps(ctx, ensure_ascii=False, indent=2))

        # What looked like a flaky empty stream was a thinking model spending
        # the whole cap on thinking (flowly/providers/background.py).
        from flowly.providers.background import BackgroundAnswerError, background_answer

        try:
            raw = await background_answer(
                self._provider, prompt, model=self._model, max_tokens=2048, temperature=0.1, label="consolidate",
            )
        except BackgroundAnswerError as exc:
            return f"Consolidation LLM call failed: {exc}"

        ops = parse_operations(raw)
        if not ops:
            return f"No consolidation operations proposed (LLM returned {len(raw)} chars)."

        if dry_run:
            lines = [
                f"- {o.op} {o.item_id}" + (f" → {o.into_id}" if o.into_id else "")
                + (f": {o.reason}" if o.reason else "")
                for o in ops
            ]
            return "Proposed (dry-run, not applied):\n" + "\n".join(lines)

        res = apply_operations(gov, ops, kg_mirror=self._facade.kg_mirror)
        if res.applied() and self._facade.memory_store is not None:
            try:
                from flowly.memory.summary import regenerate_memory_md
                regenerate_memory_md(gov, self._facade.memory_store, kg_summary=kg_summary)
            except Exception as exc:
                logger.warning(f"[consolidate] MEMORY.md refresh failed: {exc}")

        out = (
            f"Consolidated memory — merged={res.merged} superseded={res.superseded} "
            f"staled={res.staled} skipped={res.skipped}."
        )
        if res.errors:
            out += " Skipped: " + "; ".join(res.errors[:5])
        return out
