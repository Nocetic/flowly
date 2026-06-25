"""Knowledge graph tool — structured facts about people, projects, and relationships.

Stores temporal entity-relationship triples in SQLite.
No approval needed — internal memory, same level as memory_append.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool


class KnowledgeGraphTool(Tool):
    """Query and manage the knowledge graph."""

    def __init__(self, state_dir: Path):
        self._db_path = str(state_dir / "knowledge_graph.sqlite3")
        self._kg = None

    def _get_kg(self):
        if self._kg is None:
            from flowly.memory.knowledge_graph import KnowledgeGraph
            self._kg = KnowledgeGraph(self._db_path)
        return self._kg

    @property
    def name(self) -> str:
        return "knowledge_graph"

    @property
    def description(self) -> str:
        return (
            "Manage the knowledge graph — structured facts about people, companies, projects, and relationships. "
            "Actions: add (record a fact), query (look up an entity), invalidate (expire a fact), "
            "search (find by relationship type), timeline (chronological), merge (combine duplicate entities), stats. "
            "RULES: use FULL NAMES always (the full form the user gave, not a short form). ALWAYS set subject_type. "
            "ALWAYS query before adding to avoid duplicates. ONE entity per field — never comma-separated."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "query", "invalidate", "search", "timeline", "merge", "stats"],
                    "description": "Action to perform.",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject entity FULL NAME (for add/invalidate/merge). Use the full form the user provided, never a shortened version.",
                },
                "predicate": {
                    "type": "string",
                    "description": "Relationship type (for add/invalidate/search). E.g. works_at, email, lives_in, loves, uses, role, child_of.",
                },
                "object": {
                    "type": "string",
                    "description": "Object entity FULL NAME or value (for add/invalidate). For email/phone/role, this is a plain value.",
                },
                "name": {
                    "type": "string",
                    "description": "Entity name to look up (for query/timeline). Partial names work — a first name will resolve to the full-name entity.",
                },
                "valid_from": {
                    "type": "string",
                    "description": "When the fact became true, ISO date (for add).",
                },
                "ended": {
                    "type": "string",
                    "description": "When the fact stopped being true, ISO date (for invalidate).",
                },
                "as_of": {
                    "type": "string",
                    "description": "Query facts valid at this date (for query/search).",
                },
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                    "description": "Relationship direction for query (default: both).",
                },
                "subject_type": {
                    "type": "string",
                    "enum": ["person", "company", "project", "event", "unknown"],
                    "description": "Type of subject entity (for add). ALWAYS set this.",
                },
                "object_type": {
                    "type": "string",
                    "enum": ["person", "company", "project", "event", "unknown"],
                    "description": "Type of object entity (for add). Set when object is an entity, not a value like email.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        try:
            kg = self._get_kg()

            if action == "add":
                return self._add(kg, kwargs)
            elif action == "query":
                return self._query(kg, kwargs)
            elif action == "invalidate":
                return self._invalidate(kg, kwargs)
            elif action == "search":
                return self._search(kg, kwargs)
            elif action == "timeline":
                return self._timeline(kg, kwargs)
            elif action == "merge":
                return self._merge(kg, kwargs)
            elif action == "stats":
                return json.dumps(kg.stats(), indent=2)
            return f"Error: Unknown action '{action}'."
        except Exception as e:
            return f"Error: {e}"

    def _add(self, kg, kwargs: dict) -> str:
        subject = kwargs.get("subject", "").strip()
        predicate = kwargs.get("predicate", "").strip()
        obj = kwargs.get("object", "").strip()
        if not subject or not predicate or not obj:
            return "Error: 'subject', 'predicate', and 'object' are required."

        # Reject comma-separated entities
        if "," in subject:
            return "Error: ONE entity per subject. Use separate add calls for each person/entity."
        if "," in obj and predicate.lower().replace(" ", "_") not in ("email", "phone", "address", "note", "url"):
            return "Error: ONE entity per object. Use separate add calls for each person/entity."

        triple_id = kg.add_triple(
            subject=subject,
            predicate=predicate,
            obj=obj,
            valid_from=kwargs.get("valid_from"),
            confidence=kwargs.get("confidence", 1.0),
            source="agent",
            subject_type=kwargs.get("subject_type", ""),
            object_type=kwargs.get("object_type", ""),
        )
        return f"Fact added: {subject} → {predicate} → {obj} (id: {triple_id})"

    def _query(self, kg, kwargs: dict) -> str:
        name = kwargs.get("name", "").strip()
        if not name:
            return "Error: 'name' is required."

        results = kg.query_entity(
            name=name,
            as_of=kwargs.get("as_of"),
            direction=kwargs.get("direction", "both"),
        )
        if not results:
            # Suggest similar entities instead of silent failure
            suggestions = kg.suggest_entity(name)
            if suggestions:
                suggest_str = ", ".join(f"'{s}'" for s in suggestions)
                return f"No exact match for '{name}'. Did you mean: {suggest_str}? Query again with the full name."
            return f"No facts found for '{name}'. Entity does not exist in the knowledge graph."
        return json.dumps(results, indent=2, ensure_ascii=False)

    def _invalidate(self, kg, kwargs: dict) -> str:
        subject = kwargs.get("subject", "").strip()
        predicate = kwargs.get("predicate", "").strip()
        obj = kwargs.get("object", "").strip()
        if not subject or not predicate or not obj:
            return "Error: 'subject', 'predicate', and 'object' are required."

        affected = kg.invalidate(subject=subject, predicate=predicate, obj=obj, ended=kwargs.get("ended"))
        if affected:
            return f"Invalidated: {subject} → {predicate} → {obj} ({affected} fact(s) expired)"
        return f"No active fact found: {subject} → {predicate} → {obj}"

    def _search(self, kg, kwargs: dict) -> str:
        predicate = kwargs.get("predicate", "").strip()
        if not predicate:
            return "Error: 'predicate' is required."
        results = kg.query_relationship(predicate=predicate, as_of=kwargs.get("as_of"))
        if not results:
            return f"No facts found with relationship '{predicate}'."
        return json.dumps(results, indent=2, ensure_ascii=False)

    def _timeline(self, kg, kwargs: dict) -> str:
        name = kwargs.get("name", "").strip() or None
        results = kg.timeline(entity_name=name)
        if not results and name:
            suggestions = kg.suggest_entity(name)
            if suggestions:
                suggest_str = ", ".join(f"'{s}'" for s in suggestions)
                return f"No exact match for '{name}'. Did you mean: {suggest_str}?"
            return f"No timeline entries for '{name}'."
        if not results:
            return "No timeline entries."
        return json.dumps(results, indent=2, ensure_ascii=False)

    def _merge(self, kg, kwargs: dict) -> str:
        source = kwargs.get("subject", "").strip()
        target = kwargs.get("object", "").strip()
        if not source or not target:
            return "Error: 'subject' (source to merge) and 'object' (target to keep) are required."
        ok = kg.merge_entities(source, target)
        if ok:
            return f"Merged: '{source}' → '{target}'. '{source}' is now an alias for '{target}'."
        return f"Error: Could not merge. One or both entities not found."
