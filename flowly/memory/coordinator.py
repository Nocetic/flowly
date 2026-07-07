"""MemoryGovernance facade — the user/agent-facing surface over the store.

Backs the CLI (`memory list/correct/reject/undo/refresh/status/stats`) and the
`memory_recall` tool. Keeps all the lifecycle rules in one testable place so the
CLI and tool wrappers stay thin.

Privacy: ``secret`` items are never returned by recall or written to MEMORY.md.
``sensitive`` items are included in recall only when explicitly requested.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from flowly.memory.governance import (
    ACTOR_USER,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    STATUS_STALE,
    STATUS_SUPERSEDED,
    GovernanceError,
    GovernanceStore,
    MemoryItem,
)


def _injection_flagged(text: str) -> bool:
    """True if ``text`` trips the prompt-injection scanner. Fail-CLOSED: if the
    guard can't run, treat the content as flagged (→ review, never auto-active)
    rather than trusting unscanned content into live memory."""
    try:
        from flowly.cron.guard import scan_context_file
        return scan_context_file(text, "memory-live") is not None
    except Exception:
        return True


class MemoryGovernance:
    def __init__(
        self,
        gov: GovernanceStore,
        *,
        memory_store: Any = None,
        kg_mirror: Any = None,
        kg_summary_fn: Optional[Callable[[], str]] = None,
        kg_path: Optional[str] = None,
    ):
        self.gov = gov
        self.memory_store = memory_store
        self.kg_mirror = kg_mirror
        self.kg_summary_fn = kg_summary_fn
        # Path to the KG sqlite, used to materialise a triple when a
        # vault-derived fact candidate is accepted. Optional — without it,
        # accepting an Obsidian fact still activates the item, just without
        # writing into the graph.
        self.kg_path = kg_path
        # MEMORY.md regen is coalesced: ingests set this flag instead of
        # rewriting the file inline (which busts the prompt cache on every
        # write). refresh_if_dirty() regenerates once, called at end of turn.
        self._summary_dirty = False

    # -- user actions -------------------------------------------------------

    def accept(self, item_id: str) -> MemoryItem:
        """Approve a queued item: needs_review → active.

        For a vault-derived *fact* carrying a pending KG payload, accepting
        also materialises a knowledge-graph triple (source="obsidian") and
        rebinds the item's ref from ``obsidian_note`` to ``kg_triple`` so it
        behaves identically to a chat-learned fact thereafter.
        """
        item = self.gov.get_item(item_id)
        if (
            item is not None
            and item.ref_kind == "obsidian_note"
            and item.kind == "fact"
            and isinstance(item.metadata, dict)
            and isinstance(item.metadata.get("kg"), dict)
        ):
            return self._accept_obsidian_fact(item)
        return self.gov.transition(
            item_id, STATUS_ACTIVE, actor=ACTOR_USER, reason="user_accept"
        )

    def _accept_obsidian_fact(self, item: MemoryItem) -> MemoryItem:
        kg = item.metadata.get("kg") or {}
        subject = str(kg.get("subject", "")).strip()
        predicate = str(kg.get("predicate", "")).strip()
        obj = str(kg.get("object", "")).strip()
        triple_id = ""
        if subject and predicate and obj and self.kg_path:
            try:
                from flowly.memory.knowledge_graph import KnowledgeGraph
                graph = KnowledgeGraph(str(self.kg_path))
                triple_id = graph.add_triple(
                    subject, predicate, obj,
                    valid_from=kg.get("valid_from") or None,
                    confidence=float(item.confidence or 0.65),
                    source="obsidian",
                    subject_type=str(kg.get("subject_type", "") or ""),
                    object_type=str(kg.get("object_type", "") or ""),
                )
            except Exception:  # noqa: BLE001 — never lose the accept on a KG hiccup
                triple_id = ""

        key = item.normalized_key or (
            f"fact:{subject.lower()}|{predicate.lower()}" if subject and predicate else ""
        )
        # Supersede any prior active fact on the same key.
        if key:
            for sib in self.gov.find_by_key(key, statuses={STATUS_ACTIVE}):
                if sib.id == item.id or (triple_id and sib.ref_id == triple_id):
                    continue
                self.gov.transition(sib.id, STATUS_SUPERSEDED, actor=ACTOR_USER,
                                    reason="superseded_by_obsidian_fact")
                if self.kg_mirror is not None and sib.ref_kind == "kg_triple" and sib.ref_id:
                    self.kg_mirror.supersede(sib.ref_id)

        if triple_id:
            self.gov.update_fields(item.id, ref_kind="kg_triple", ref_id=triple_id)
        active = self.gov.transition(
            item.id, STATUS_ACTIVE, actor=ACTOR_USER, reason="user_accept_obsidian_fact"
        )
        self._summary_dirty = True
        self.mark_dirty()
        return active

    def reject(self, item_id: str) -> MemoryItem:
        """User rejects an item (any non-terminal state) → rejected."""
        item = self.gov.get_item(item_id)
        if item is None:
            raise GovernanceError(f"item not found: {item_id}")
        return self.gov.transition(
            item_id, STATUS_REJECTED, actor=ACTOR_USER, reason="user_reject"
        )

    def correct(
        self, item_id: str, new_text: str, *, confidence: Optional[float] = None
    ) -> MemoryItem:
        """User edits an item's text; a corrected item becomes active."""
        item = self.gov.get_item(item_id)
        if item is None:
            raise GovernanceError(f"item not found: {item_id}")
        fields: dict[str, Any] = {"text": new_text}
        if confidence is not None:
            fields["confidence"] = confidence
        self.gov.update_fields(item_id, **fields)
        if item.status in (STATUS_NEEDS_REVIEW, STATUS_CANDIDATE):
            try:
                self.gov.transition(
                    item_id, STATUS_ACTIVE, actor=ACTOR_USER, reason="user_correct"
                )
            except GovernanceError:
                pass
        return self.gov.get_item(item_id)  # type: ignore[return-value]

    def undo(self, item_id: str) -> MemoryItem:
        """Restore a superseded/stale item to active (rollback a bad supersede).

        Demotes any other active item sharing its key to superseded so the
        "one active per key" invariant holds, and mirrors both moves into the KG
        (restore this item's triple, close the demoted sibling's).
        """
        item = self.gov.get_item(item_id)
        if item is None:
            raise GovernanceError(f"item not found: {item_id}")
        if item.status not in (STATUS_SUPERSEDED, STATUS_STALE):
            raise GovernanceError(
                f"undo only applies to superseded/stale items (got {item.status})"
            )
        # Demote current active sibling(s) on the same key.
        if item.normalized_key:
            for sib in self.gov.find_by_key(item.normalized_key, statuses={STATUS_ACTIVE}):
                if sib.id == item_id:
                    continue
                self.gov.transition(
                    sib.id, STATUS_SUPERSEDED, actor=ACTOR_USER,
                    reason="demoted_by_undo", supersedes=item_id,
                )
                if self.kg_mirror is not None and sib.ref_kind == "kg_triple" and sib.ref_id:
                    self.kg_mirror.supersede(sib.ref_id)
        restored = self.gov.transition(
            item_id, STATUS_ACTIVE, actor=ACTOR_USER, reason="user_undo"
        )
        if self.kg_mirror is not None and item.ref_kind == "kg_triple" and item.ref_id:
            self.kg_mirror.restore(item.ref_id)
        return restored

    # -- reads --------------------------------------------------------------

    def list_items(self, *, status: Optional[str] = None) -> list[MemoryItem]:
        return self.gov.list_items(status=status)

    def review_queue(self) -> list[MemoryItem]:
        return self.gov.list_items(status=STATUS_NEEDS_REVIEW)

    def recall(
        self, *, include_sensitive: bool = False, limit: Optional[int] = None
    ) -> dict[str, Any]:
        """Active memory for the agent. Excludes ``secret`` always; includes
        ``sensitive`` only when asked. Returns items with provenance + KG."""
        items = self.gov.list_items(status=STATUS_ACTIVE)
        # Highest-trust first so feedback-adjusted confidence changes what the
        # agent sees first; apply the limit AFTER ordering.
        items.sort(key=lambda x: (-x.confidence, x.text.lower()))
        if limit is not None:
            items = items[:limit]
        out = []
        for it in items:
            if it.privacy_level == "secret":
                continue
            if it.privacy_level == "sensitive" and not include_sensitive:
                continue
            self.gov.touch_used(it.id)
            out.append({
                "id": it.id,
                "kind": it.kind,
                "text": it.text,
                "confidence": it.confidence,
                "source_session": it.source_session,
                "source_message_ids": it.source_message_ids,
                "ref": {"kind": it.ref_kind, "id": it.ref_id},
            })
        kg_summary = self.kg_summary_fn() if self.kg_summary_fn else ""
        return {"items": out, "kg_summary": kg_summary, "count": len(out)}

    # Trust-feedback confidence nudges (post-hoc; separate from calibration which
    # runs at ingest). Below this floor an active item is demoted to review.
    _FEEDBACK_HELPFUL = 0.10
    _FEEDBACK_UNHELPFUL = -0.15
    _REVIEW_FLOOR = 0.55

    def ingest_feedback(
        self, item_id: str, helpful: bool, note: str = ""
    ) -> Optional[MemoryItem]:
        """Record a helpful/unhelpful signal on a recalled item and nudge its
        confidence. If confidence falls below the review floor, demote the item
        from active to needs_review (audited). Returns the updated item."""
        item = self.gov.get_item(item_id)
        if item is None:
            raise GovernanceError(f"item not found: {item_id}")
        self.gov.add_feedback(item_id, helpful, note)
        delta = self._FEEDBACK_HELPFUL if helpful else self._FEEDBACK_UNHELPFUL
        new_conf = max(0.0, min(1.0, item.confidence + delta))
        self.gov.update_fields(item_id, confidence=new_conf)
        if not helpful and new_conf < self._REVIEW_FLOOR and item.status == STATUS_ACTIVE:
            self.gov.transition(
                item_id, STATUS_NEEDS_REVIEW, actor=ACTOR_USER,
                reason=f"feedback_below_floor ({new_conf:.2f})",
            )
        self._summary_dirty = True   # ordering/demotion changed; regen at turn end
        return self.gov.get_item(item_id)

    def stats(self) -> dict[str, Any]:
        return self.gov.stats()

    # -- dirty tracking (drives autonomous consolidation) ------------------

    _DIRTY_KEY = "consolidate_dirty"

    def mark_dirty(self) -> None:
        self.gov.set_meta(self._DIRTY_KEY, "1")

    def is_dirty(self) -> bool:
        return self.gov.get_meta(self._DIRTY_KEY) == "1"

    def clear_dirty(self) -> None:
        self.gov.set_meta(self._DIRTY_KEY, "")

    # -- live ingest (post_tool_call hook) ---------------------------------

    @staticmethod
    def _slug(text: str) -> str:
        import re
        words = re.findall(r"[a-z0-9]+", text.lower())[:6]
        return "-".join(words)

    def ingest_append(
        self, content: str, *, source_session: str = "", auto_activate: bool = True
    ) -> Optional[MemoryItem]:
        """Record a memory_append as a governed item + refresh MEMORY.md.

        Deduplicates against existing active items (same normalized text).
        Returns the new item, or None if it was a duplicate.

        ``auto_activate`` (default True) keeps the trusted behaviour for real
        user-channel writes: the agent chose to save it → active. Set it False
        for autonomous/background runs (heartbeat/cron/subagent) — there the
        "decision" is the agent's own inference, so the item parks in
        ``needs_review`` instead of silently becoming active memory. A write
        that trips the prompt-injection scanner is always routed to review too,
        never auto-activated.
        """
        content = (content or "").strip()
        if not content:
            return None
        norm = " ".join(content.split()).lower()
        for it in self.gov.list_items(status=STATUS_ACTIVE):
            if " ".join(it.text.split()).lower() == norm:
                self.gov.touch_seen(it.id)
                return None
        flagged = _injection_flagged(content)
        item = self.gov.add_item(
            kind="preference", text=content, status="candidate",
            ref_kind="memory_md", normalized_key="pref:" + self._slug(content),
            confidence=0.85, source_session=source_session,
            actor="system", reason="memory_append",
        )
        if auto_activate and not flagged:
            self.gov.transition(item.id, STATUS_ACTIVE, actor="system", reason="memory_append")
        else:
            reason = "injection_flagged" if flagged else "unreviewed_autonomous_write"
            self.gov.transition(item.id, STATUS_NEEDS_REVIEW, actor="system", reason=reason)
        self._summary_dirty = True   # coalesced regen at end of turn
        self.mark_dirty()
        return self.gov.get_item(item.id)

    def ingest_kg_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        triple_id: str,
        *,
        source_session: str = "",
        auto_activate: bool = True,
    ) -> Optional[MemoryItem]:
        """Record a knowledge_graph add as a governed fact item.

        If an active fact with the same subject+predicate already exists with a
        different triple, supersede it (and close its KG triple) so the new fact
        wins — the live, chat-visible version of the dreamer's arbitration. The
        supersede only happens when the newcomer itself activates: an unreviewed
        autonomous fact must not evict a known one before the user sees it.

        ``auto_activate``/injection handling mirror :meth:`ingest_append`.
        """
        subject, obj = subject.strip(), obj.strip()
        # Skip self-referential garbage (e.g. the agent set subject=object=email
        # before it knew the entity's name). Don't pollute governance with it.
        if not subject or not obj or subject.lower() == obj.lower():
            return None
        text = f"{subject} {predicate.replace('_', ' ')} {obj}".strip()
        key = f"fact:{subject.lower()}|{predicate.strip().lower()}"
        # Already recorded this exact triple?
        if triple_id and self.gov.find_by_ref("kg_triple", triple_id):
            return None
        flagged = _injection_flagged(text)
        activate = auto_activate and not flagged
        # Supersede any active fact on the same key with a different triple —
        # only when the newcomer is actually activating.
        if activate:
            for sib in self.gov.find_by_key(key, statuses={STATUS_ACTIVE}):
                if sib.ref_id == triple_id:
                    continue
                self.gov.transition(sib.id, STATUS_SUPERSEDED, actor="system",
                                    reason="superseded_by_newer_kg_fact")
                if self.kg_mirror is not None and sib.ref_kind == "kg_triple" and sib.ref_id:
                    self.kg_mirror.supersede(sib.ref_id)
        item = self.gov.add_item(
            kind="fact", text=text, status="candidate",
            ref_kind="kg_triple", ref_id=triple_id or None, normalized_key=key,
            confidence=0.9, source_session=source_session,
            actor="system", reason="knowledge_graph_add",
        )
        if activate:
            self.gov.transition(item.id, STATUS_ACTIVE, actor="system",
                                reason="knowledge_graph_add")
        else:
            reason = "injection_flagged" if flagged else "unreviewed_autonomous_write"
            self.gov.transition(item.id, STATUS_NEEDS_REVIEW, actor="system", reason=reason)
        self._summary_dirty = True   # coalesced regen at end of turn
        self.mark_dirty()
        return self.gov.get_item(item.id)

    def ingest_obsidian_candidate(
        self,
        *,
        kind: str,
        text: str,
        path: str,
        source_lines: str = "",
        confidence: float = 0.65,
        privacy_level: str = "normal",
        kg: Optional[dict[str, Any]] = None,
        source_session: str = "",
    ) -> MemoryItem:
        """Record a vault-derived candidate as ``needs_review`` (never auto-active).

        ``path``/``source_lines`` are stored as provenance. For ``fact`` kinds a
        ``kg`` payload may be attached; it is materialised into the knowledge
        graph only if and when the user accepts the item (see ``accept``).
        Nothing here enters recall until approved.
        """
        text = (text or "").strip()
        if not text:
            raise GovernanceError("empty candidate text")
        metadata: dict[str, Any] = {"source": "obsidian", "path": path}
        if source_lines:
            metadata["lines"] = source_lines
        key = ""
        if kind == "fact" and isinstance(kg, dict):
            metadata["kg"] = kg
            subj = str(kg.get("subject", "")).strip().lower()
            pred = str(kg.get("predicate", "")).strip().lower()
            if subj and pred:
                key = f"fact:{subj}|{pred}"
        ref_id = f"{path}#{source_lines}" if source_lines else path
        item = self.gov.add_item(
            kind=kind, text=text, status=STATUS_NEEDS_REVIEW,
            ref_kind="obsidian_note", ref_id=ref_id, normalized_key=key,
            confidence=confidence, privacy_level=privacy_level,
            source_session=source_session, metadata=metadata,
            actor="system", reason="obsidian_ingest",
        )
        return item

    # -- maintenance --------------------------------------------------------

    def refresh(self) -> Optional[str]:
        """Regenerate the MEMORY.md generated block from current active items."""
        if self.memory_store is None:
            return None
        from flowly.memory.summary import regenerate_memory_md
        kg_summary = self.kg_summary_fn() if self.kg_summary_fn else ""
        self._summary_dirty = False
        return regenerate_memory_md(self.gov, self.memory_store, kg_summary=kg_summary)

    def refresh_if_dirty(self) -> Optional[str]:
        """Regenerate MEMORY.md once iff an ingest marked it dirty this turn.

        Called once at the end of an agent turn so a turn with N memory writes
        rewrites MEMORY.md a single time instead of N times — the per-write
        rewrites otherwise bust the Anthropic prefix cache on every ingest.
        """
        if not self._summary_dirty:
            return None
        return self.refresh()
