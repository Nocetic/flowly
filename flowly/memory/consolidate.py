"""Memory consolidation — LLM-proposed, governance-applied cleanup.

The live ingest hook is deliberately dumb (it records what the agent writes). The
*semantic* cleanup — merging the same fact recorded under different keys, marking
free-form notes that just duplicate KG facts as superseded, flagging free-form
that references now-outdated info — needs judgement, so it runs as a separate
consolidation pass.

Split for testability:
* ``build_context`` / ``apply_operations`` are deterministic and fully tested.
* the LLM only *proposes* ``ConsolidateOp``s; it never writes. ``apply_operations``
  validates every op against the live store (must target an existing active item,
  never deletes) and records an audit row per move.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger

from flowly.memory.governance import (
    ACTOR_SYSTEM,
    STATUS_ACTIVE,
    STATUS_STALE,
    STATUS_SUPERSEDED,
    GovernanceError,
    GovernanceStore,
)

OP_SUPERSEDE = "supersede"   # redundant fact/preference → superseded (optionally into_id)
OP_STALE = "stale"           # references outdated info → stale
OP_MERGE = "merge"           # same fact under a different key → superseded into_id
VALID_OPS = frozenset({OP_SUPERSEDE, OP_STALE, OP_MERGE})


@dataclass
class ConsolidateOp:
    op: str
    item_id: str
    into_id: Optional[str] = None  # survivor for merge/supersede links
    reason: str = ""


@dataclass
class ConsolidateResult:
    proposed: int = 0
    superseded: int = 0
    staled: int = 0
    merged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def applied(self) -> int:
        return self.superseded + self.staled + self.merged


def build_context(gov: GovernanceStore, kg_summary: str = "") -> dict[str, Any]:
    """Snapshot of active items + KG for the proposer LLM."""
    items = [
        {
            "id": it.id, "kind": it.kind, "text": it.text,
            "normalized_key": it.normalized_key, "confidence": it.confidence,
            "ref_kind": it.ref_kind, "ref_id": it.ref_id,
        }
        for it in gov.list_items(status=STATUS_ACTIVE)
    ]
    return {"items": items, "kg_summary": kg_summary}


PROMPT = """You consolidate an agent's long-term memory. You are given the
currently ACTIVE memory items (some structured 'fact' items backed by a knowledge
graph, some free-form 'preference' items) and a KG summary.

Propose cleanup operations. Be CONSERVATIVE — when unsure, do nothing.

Operations (JSON):
- {"op":"merge","item_id":"<loser>","into_id":"<survivor>","reason":"..."}
    Two items describe the SAME real-world fact under different keys (e.g. the
    same email recorded once under the email-as-subject and once under the
    person). Keep the more correct/specific one as into_id; the loser is retired.
- {"op":"supersede","item_id":"<id>","reason":"..."}
    A free-form preference whose information is ALREADY fully captured by KG
    facts (pure duplication). Retire the redundant free-form item.
- {"op":"stale","item_id":"<id>","reason":"..."}
    A free-form item that references now-OUTDATED info (contradicted by a newer
    active fact, e.g. it cites an old email/role).

Rules:
- Only use item ids present in the input.
- NEVER retire a unique, current fact. Prefer keeping structured facts over
  free-form when they overlap.
- Output ONLY JSON: {"operations":[ ... ]}. Empty list if nothing to do.

INPUT:
{context}
"""


def parse_operations(raw: str) -> list[ConsolidateOp]:
    """Parse the LLM's JSON into validated ops (best-effort, never raises)."""
    text = (raw or "").strip()
    # tolerate ```json fences
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    ops = []
    for o in data.get("operations", []):
        if not isinstance(o, dict):
            continue
        op = o.get("op")
        item_id = o.get("item_id")
        if op in VALID_OPS and item_id:
            ops.append(ConsolidateOp(
                op=op, item_id=item_id, into_id=o.get("into_id"),
                reason=str(o.get("reason", "")),
            ))
    return ops


def apply_operations(
    gov: GovernanceStore,
    ops: list[ConsolidateOp],
    *,
    kg_mirror: Any = None,
) -> ConsolidateResult:
    """Apply proposed ops to the store. Validates each op; never deletes.

    A move only happens if the target is currently active; merges require a valid,
    distinct, active survivor. Every applied move is audited by the store.
    """
    res = ConsolidateResult(proposed=len(ops))
    for op in ops:
        item = gov.get_item(op.item_id)
        if item is None or item.status != STATUS_ACTIVE:
            res.skipped += 1
            res.errors.append(f"{op.item_id}: not an active item")
            continue
        try:
            if op.op == OP_STALE:
                gov.transition(op.item_id, STATUS_STALE, actor=ACTOR_SYSTEM,
                               reason=f"consolidate: {op.reason}"[:200])
                res.staled += 1
            else:  # supersede / merge
                survivor = None
                if op.op == OP_MERGE:
                    if not op.into_id or op.into_id == op.item_id:
                        res.skipped += 1
                        res.errors.append(f"{op.item_id}: merge needs a distinct into_id")
                        continue
                    survivor = gov.get_item(op.into_id)
                    if survivor is None or survivor.status != STATUS_ACTIVE:
                        res.skipped += 1
                        res.errors.append(f"{op.item_id}: merge survivor not active")
                        continue
                gov.transition(op.item_id, STATUS_SUPERSEDED, actor=ACTOR_SYSTEM,
                               reason=f"consolidate: {op.reason}"[:200],
                               supersedes=op.into_id)
                if kg_mirror is not None and item.ref_kind == "kg_triple" and item.ref_id:
                    kg_mirror.supersede(item.ref_id)
                res.merged += 1 if op.op == OP_MERGE else 0
                res.superseded += 1 if op.op == OP_SUPERSEDE else 0
        except GovernanceError as exc:
            res.skipped += 1
            res.errors.append(f"{op.item_id}: {exc}")
    return res


class Consolidator:
    """Ties context → propose (LLM) → apply → refresh. ``propose_fn`` is injected
    so the deterministic parts are testable with a fake proposer."""

    def __init__(
        self,
        gov: GovernanceStore,
        propose_fn: Callable[[dict[str, Any]], list[ConsolidateOp]],
        *,
        kg_mirror: Any = None,
        memory_store: Any = None,
        kg_summary_fn: Optional[Callable[[], str]] = None,
    ):
        self.gov = gov
        self.propose_fn = propose_fn
        self.kg_mirror = kg_mirror
        self.memory_store = memory_store
        self.kg_summary_fn = kg_summary_fn

    def run(self, *, dry_run: bool = False) -> tuple[list[ConsolidateOp], ConsolidateResult]:
        kg_summary = self.kg_summary_fn() if self.kg_summary_fn else ""
        ctx = build_context(self.gov, kg_summary)
        ops = self.propose_fn(ctx)
        if dry_run:
            return ops, ConsolidateResult(proposed=len(ops))
        res = apply_operations(self.gov, ops, kg_mirror=self.kg_mirror)
        if res.applied() and self.memory_store is not None:
            try:
                from flowly.memory.summary import regenerate_memory_md
                regenerate_memory_md(self.gov, self.memory_store, kg_summary=kg_summary)
            except Exception as exc:
                logger.warning(f"[consolidate] MEMORY.md refresh failed: {exc}")
        return ops, res
