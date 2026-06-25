"""obsidian_ingest tool — turn vault note content into review-gated candidates.

The model extracts discrete memory items from a note (preferences, profile
facts, project notes, relationships, …) and submits them here. Every item is
recorded as ``needs_review`` in governance — nothing enters recall or the
knowledge graph until the user explicitly accepts it. Fact items may carry a
``kg`` payload that is materialised into the graph only on acceptance.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.memory.governance import VALID_KINDS, VALID_PRIVACY
from flowly.obsidian.tools import ObsidianRuntime
from flowly.obsidian.vault import VaultError, read_note


class ObsidianIngestTool(Tool):
    """Submit vault-derived memory candidates for user review."""

    def __init__(self, rt: ObsidianRuntime, facade: Any, *, policy: str = "review_gated") -> None:
        self._rt = rt
        self._facade = facade          # MemoryGovernance coordinator
        self._policy = policy

    @property
    def name(self) -> str:
        return "obsidian_ingest"

    @property
    def description(self) -> str:
        return (
            "Extract durable facts/preferences/profile details from an Obsidian "
            "note and submit them as memory candidates for the user to review. "
            "Use after reading a note the user wants remembered. Nothing is saved "
            "to memory automatically — every item awaits user approval. For 'fact' "
            "items, include a 'kg' triple {subject,predicate,object} when possible."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative note path the items came from."},
                "items": {
                    "type": "array",
                    "description": "Candidate memory items extracted from the note.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": sorted(VALID_KINDS),
                                "description": "Memory kind.",
                            },
                            "text": {"type": "string", "description": "Human-readable memory text."},
                            "confidence": {"type": "number", "description": "0..1 (default 0.65)."},
                            "privacy_level": {
                                "type": "string",
                                "enum": sorted(VALID_PRIVACY),
                                "description": "normal | sensitive | secret (default normal).",
                            },
                            "source_lines": {"type": "string", "description": "Citation, e.g. 'L12-L18'."},
                            "kg": {
                                "type": "object",
                                "description": "For facts: {subject,predicate,object[,subject_type,object_type,valid_from]}.",
                            },
                        },
                        "required": ["kind", "text"],
                    },
                },
            },
            "required": ["path", "items"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if self._facade is None:
            return json.dumps({"ok": False, "error": "memory_disabled",
                               "detail": "Governed memory is not enabled; cannot ingest."})
        try:
            self._rt.root()
        except Exception:
            return json.dumps({"ok": False, "error": "not_configured",
                               "detail": "Obsidian vault is not configured or not found."})
        if self._policy == "manual_only":
            # manual_only still allows the explicit tool; it only disables the
            # automatic injection-time prompting. (kept for clarity / future use)
            pass

        path = (kwargs.get("path") or "").strip()
        items = kwargs.get("items") or []
        if not path:
            return json.dumps({"ok": False, "error": "error", "detail": "path is required"})
        if not isinstance(items, list) or not items:
            return json.dumps({"ok": False, "error": "error", "detail": "items must be a non-empty list"})

        # Validate the note exists/readable (best-effort; provenance must be real).
        try:
            read_note(self._rt.root(), path, max_note_bytes=self._rt.max_note_bytes)
        except VaultError as exc:
            return json.dumps({"ok": False, "error": "error", "detail": str(exc)})

        created: list[dict[str, Any]] = []
        errors: list[str] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            try:
                item = self._facade.ingest_obsidian_candidate(
                    kind=str(raw.get("kind", "")).strip(),
                    text=str(raw.get("text", "")),
                    path=path,
                    source_lines=str(raw.get("source_lines", "") or ""),
                    confidence=float(raw.get("confidence", 0.65) or 0.65),
                    privacy_level=str(raw.get("privacy_level", "normal") or "normal"),
                    kg=raw.get("kg") if isinstance(raw.get("kg"), dict) else None,
                )
                created.append({"id": item.id, "kind": item.kind, "text": item.text,
                                "status": item.status})
            except Exception as exc:  # noqa: BLE001 — collect per-item errors
                errors.append(f"{raw.get('text', '?')[:40]}: {exc}")

        return json.dumps({
            "ok": True,
            "created": created,
            "count": len(created),
            "errors": errors,
            "note": "Items are pending your review — accept them in memory review to save.",
        })
