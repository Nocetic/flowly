"""Temporal knowledge graph — structured facts about people, projects, and relationships.

SQLite-based, adapted from MemPalace. Features:
- Entity aliases ("Berke" → "Berke Toprak")
- Fuzzy name resolution (partial match)
- Value predicates (email, phone) stored as properties, not entity nodes
- Temporal validity (valid_from/valid_to)
- Entity merge (combine duplicates)

Usage:
    kg = KnowledgeGraph("/path/to/knowledge_graph.sqlite3")
    kg.add_triple("Hakan Ören", "works_at", "Nocetic Limited", subject_type="person", object_type="company")
    kg.query_entity("Hakan")  # resolves via alias/fuzzy to "Hakan Ören"
"""

import hashlib
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path


# Predicates where the object is a value (not an entity)
VALUE_PREDICATES = frozenset({
    "email", "phone", "url", "website", "address", "location",
    "birthday", "age", "role", "title", "salary", "note",
})


class KnowledgeGraph:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT DEFAULT 'unknown',
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                confidence REAL DEFAULT 1.0,
                source TEXT DEFAULT 'agent',
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (subject) REFERENCES entities(id)
            );

            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT PRIMARY KEY,
                canonical_id TEXT NOT NULL,
                FOREIGN KEY (canonical_id) REFERENCES entities(id)
            );

            CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
            CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
            CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
            CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);
            CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON aliases(canonical_id);
        """)
        conn.commit()
        conn.close()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    @staticmethod
    def _normalize_id(name: str) -> str:
        return name.lower().strip().replace(" ", "_").replace("'", "")

    # ── Name Resolution ──────────────────────────────────────────────────

    def resolve_entity(self, name: str, conn: sqlite3.Connection | None = None) -> tuple[str, str] | None:
        """Resolve a name to (entity_id, canonical_name).

        Resolution: exact ID match → alias match. No fuzzy.
        Returns None if not found.
        """
        own_conn = conn is None
        if own_conn:
            conn = self._conn()

        nid = self._normalize_id(name)

        # 1. Exact match
        row = conn.execute("SELECT id, name FROM entities WHERE id = ?", (nid,)).fetchone()
        if row:
            if own_conn:
                conn.close()
            return (row[0], row[1])

        # 2. Alias match
        row = conn.execute(
            "SELECT e.id, e.name FROM aliases a JOIN entities e ON a.canonical_id = e.id WHERE a.alias = ?",
            (nid,),
        ).fetchone()
        if row:
            if own_conn:
                conn.close()
            return (row[0], row[1])

        if own_conn:
            conn.close()
        return None

    def suggest_entity(self, name: str) -> list[str]:
        """Suggest matching entity names for a partial/ambiguous name.

        Used by the tool to say 'Did you mean...?' — never auto-resolves.
        """
        conn = self._conn()
        pattern = f"%{name.strip()}%"
        rows = conn.execute(
            "SELECT DISTINCT name FROM entities WHERE name LIKE ? COLLATE NOCASE LIMIT 5",
            (pattern,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    # ── Write ────────────────────────────────────────────────────────────

    def add_entity(self, name: str, entity_type: str = "unknown", properties: dict | None = None) -> str:
        """Add or update an entity. If entity exists (by resolve), updates type/properties."""
        conn = self._conn()
        resolved = self.resolve_entity(name, conn)

        if resolved:
            eid, _ = resolved
            # Update type if upgrading from unknown
            if entity_type != "unknown":
                conn.execute("UPDATE entities SET type = ? WHERE id = ? AND type = 'unknown'", (entity_type, eid))
            if properties:
                conn.execute("UPDATE entities SET properties = ? WHERE id = ?", (json.dumps(properties), eid))
            conn.commit()
            conn.close()
            return eid

        eid = self._normalize_id(name)
        props = json.dumps(properties or {})
        conn.execute(
            "INSERT OR REPLACE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
            (eid, name.strip(), entity_type, props),
        )
        conn.commit()
        conn.close()
        return eid

    def add_alias(self, alias: str, canonical_name: str) -> bool:
        """Register an alias for an existing entity."""
        conn = self._conn()
        resolved = self.resolve_entity(canonical_name, conn)
        if not resolved:
            conn.close()
            return False

        canonical_id = resolved[0]
        alias_id = self._normalize_id(alias)
        conn.execute("INSERT OR REPLACE INTO aliases (alias, canonical_id) VALUES (?, ?)", (alias_id, canonical_id))
        conn.commit()
        conn.close()
        return True

    def add_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
        source: str = "agent",
        subject_type: str = "",
        object_type: str = "",
    ) -> str:
        pred = predicate.lower().strip().replace(" ", "_")
        is_value = pred in VALUE_PREDICATES

        conn = self._conn()

        # Resolve or create subject entity
        sub_resolved = self.resolve_entity(subject, conn)
        if sub_resolved:
            sub_id, sub_canonical = sub_resolved
            # Update type if provided and currently unknown
            if subject_type and subject_type != "unknown":
                conn.execute("UPDATE entities SET type = ? WHERE id = ? AND type = 'unknown'", (subject_type, sub_id))
        else:
            sub_id = self._normalize_id(subject)
            sub_canonical = subject.strip()
            stype = subject_type if subject_type else "unknown"
            conn.execute("INSERT OR IGNORE INTO entities (id, name, type) VALUES (?, ?, ?)", (sub_id, sub_canonical, stype))

        # For value predicates, object is stored as plain text (not an entity)
        if is_value:
            obj_id = self._normalize_id(obj)
        else:
            # Resolve or create object entity
            obj_resolved = self.resolve_entity(obj, conn)
            if obj_resolved:
                obj_id = obj_resolved[0]
                if object_type and object_type != "unknown":
                    conn.execute("UPDATE entities SET type = ? WHERE id = ? AND type = 'unknown'", (object_type, obj_id))
            else:
                obj_id = self._normalize_id(obj)
                otype = object_type if object_type else "unknown"
                conn.execute("INSERT OR IGNORE INTO entities (id, name, type) VALUES (?, ?, ?)", (obj_id, obj.strip(), otype))

        # Duplicate check
        existing = conn.execute(
            "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            (sub_id, pred, obj_id),
        ).fetchone()
        if existing:
            conn.commit()
            conn.close()
            return existing[0]

        triple_id = (
            f"t_{sub_id}_{pred}_{obj_id[:20]}_"
            f"{hashlib.md5(f'{valid_from}{datetime.now().isoformat()}'.encode()).hexdigest()[:8]}"
        )

        conn.execute(
            """INSERT INTO triples (id, subject, predicate, object, valid_from, valid_to, confidence, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (triple_id, sub_id, pred, obj_id, valid_from, valid_to, confidence, source),
        )
        conn.commit()
        conn.close()
        return triple_id

    def invalidate(self, subject: str, predicate: str, obj: str, ended: str | None = None) -> int:
        conn = self._conn()
        sub_resolved = self.resolve_entity(subject, conn)
        sub_id = sub_resolved[0] if sub_resolved else self._normalize_id(subject)

        pred = predicate.lower().strip().replace(" ", "_")

        if pred in VALUE_PREDICATES:
            obj_id = self._normalize_id(obj)
        else:
            obj_resolved = self.resolve_entity(obj, conn)
            obj_id = obj_resolved[0] if obj_resolved else self._normalize_id(obj)

        ended = ended or date.today().isoformat()

        cursor = conn.execute(
            "UPDATE triples SET valid_to=? WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            (ended, sub_id, pred, obj_id),
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected

    def merge_entities(self, source_name: str, target_name: str) -> bool:
        """Merge source entity into target. Source becomes an alias of target."""
        conn = self._conn()
        src = self.resolve_entity(source_name, conn)
        tgt = self.resolve_entity(target_name, conn)

        if not src or not tgt:
            conn.close()
            return False
        if src[0] == tgt[0]:
            conn.close()
            return True  # Already the same

        src_id, src_name = src
        tgt_id, tgt_name = tgt

        # Move all triples from source to target
        conn.execute("UPDATE triples SET subject = ? WHERE subject = ?", (tgt_id, src_id))
        conn.execute("UPDATE triples SET object = ? WHERE object = ?", (tgt_id, src_id))

        # Register source as alias
        conn.execute("INSERT OR REPLACE INTO aliases (alias, canonical_id) VALUES (?, ?)", (src_id, tgt_id))

        # Move any aliases pointing to source
        conn.execute("UPDATE aliases SET canonical_id = ? WHERE canonical_id = ?", (tgt_id, src_id))

        # Delete source entity
        conn.execute("DELETE FROM entities WHERE id = ?", (src_id,))

        # Remove duplicate triples that may have been created by the merge
        conn.execute("""
            DELETE FROM triples WHERE id NOT IN (
                SELECT MIN(id) FROM triples
                GROUP BY subject, predicate, object, valid_to
            )
        """)

        conn.commit()
        conn.close()
        return True

    # ── Read ─────────────────────────────────────────────────────────────

    def query_entity(self, name: str, as_of: str | None = None, direction: str = "both") -> list[dict]:
        conn = self._conn()
        resolved = self.resolve_entity(name, conn)

        if not resolved:
            conn.close()
            return []

        eid, canonical_name = resolved
        results = []

        if direction in ("outgoing", "both"):
            query = """SELECT t.predicate, t.object, t.valid_from, t.valid_to, t.confidence,
                              COALESCE(e.name, t.object) as obj_display
                       FROM triples t
                       LEFT JOIN entities e ON t.object = e.id
                       WHERE t.subject = ?"""
            params: list = [eid]
            if as_of:
                query += " AND (t.valid_from IS NULL OR t.valid_from <= ?) AND (t.valid_to IS NULL OR t.valid_to >= ?)"
                params.extend([as_of, as_of])
            for row in conn.execute(query, params).fetchall():
                results.append({
                    "direction": "outgoing",
                    "subject": canonical_name,
                    "predicate": row[0],
                    "object": row[5],
                    "valid_from": row[2],
                    "valid_to": row[3],
                    "confidence": row[4],
                    "current": row[3] is None,
                })

        if direction in ("incoming", "both"):
            query = """SELECT t.predicate, e.name as sub_name, t.valid_from, t.valid_to, t.confidence
                       FROM triples t
                       JOIN entities e ON t.subject = e.id
                       WHERE t.object = ?"""
            params = [eid]
            if as_of:
                query += " AND (t.valid_from IS NULL OR t.valid_from <= ?) AND (t.valid_to IS NULL OR t.valid_to >= ?)"
                params.extend([as_of, as_of])
            for row in conn.execute(query, params).fetchall():
                results.append({
                    "direction": "incoming",
                    "subject": row[1],
                    "predicate": row[0],
                    "object": canonical_name,
                    "valid_from": row[2],
                    "valid_to": row[3],
                    "confidence": row[4],
                    "current": row[3] is None,
                })

        conn.close()
        return results

    def query_relationship(self, predicate: str, as_of: str | None = None) -> list[dict]:
        pred = predicate.lower().strip().replace(" ", "_")
        conn = self._conn()
        is_value = pred in VALUE_PREDICATES

        if is_value:
            query = """SELECT e.name as sub_name, t.object as obj_display, t.valid_from, t.valid_to
                       FROM triples t JOIN entities e ON t.subject = e.id
                       WHERE t.predicate = ?"""
        else:
            query = """SELECT s.name as sub_name, COALESCE(o.name, t.object) as obj_display, t.valid_from, t.valid_to
                       FROM triples t
                       JOIN entities s ON t.subject = s.id
                       LEFT JOIN entities o ON t.object = o.id
                       WHERE t.predicate = ?"""

        params: list = [pred]
        if as_of:
            query += " AND (t.valid_from IS NULL OR t.valid_from <= ?) AND (t.valid_to IS NULL OR t.valid_to >= ?)"
            params.extend([as_of, as_of])

        results = []
        for row in conn.execute(query, params).fetchall():
            results.append({
                "subject": row[0],
                "predicate": pred,
                "object": row[1],
                "valid_from": row[2],
                "valid_to": row[3],
                "current": row[3] is None,
            })
        conn.close()
        return results

    def timeline(self, entity_name: str | None = None) -> list[dict]:
        conn = self._conn()
        if entity_name:
            resolved = self.resolve_entity(entity_name, conn)
            if not resolved:
                conn.close()
                return []
            eid = resolved[0]
            rows = conn.execute("""
                SELECT e.name as sub_name, t.predicate, COALESCE(o.name, t.object) as obj_display,
                       t.valid_from, t.valid_to
                FROM triples t
                JOIN entities e ON t.subject = e.id
                LEFT JOIN entities o ON t.object = o.id
                WHERE (t.subject = ? OR t.object = ?)
                ORDER BY t.valid_from ASC NULLS LAST
            """, (eid, eid)).fetchall()
        else:
            rows = conn.execute("""
                SELECT s.name as sub_name, t.predicate, COALESCE(o.name, t.object) as obj_display,
                       t.valid_from, t.valid_to
                FROM triples t
                JOIN entities s ON t.subject = s.id
                LEFT JOIN entities o ON t.object = o.id
                ORDER BY t.valid_from ASC NULLS LAST
                LIMIT 100
            """).fetchall()

        conn.close()
        return [
            {"subject": r[0], "predicate": r[1], "object": r[2],
             "valid_from": r[3], "valid_to": r[4], "current": r[4] is None}
            for r in rows
        ]

    # ── Stats & Summary ──────────────────────────────────────────────────

    def stats(self) -> dict:
        conn = self._conn()
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        current = conn.execute("SELECT COUNT(*) FROM triples WHERE valid_to IS NULL").fetchone()[0]
        aliases = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
        predicates = [
            r[0] for r in conn.execute("SELECT DISTINCT predicate FROM triples ORDER BY predicate").fetchall()
        ]
        conn.close()
        return {
            "entities": entities,
            "triples": triples,
            "current_facts": current,
            "expired_facts": triples - current,
            "aliases": aliases,
            "relationship_types": predicates,
        }

    def summary(self, max_entities: int = 20) -> str:
        """Compact text summary for system prompt injection.

        Format:
        - Alice (person): email=alice@example.com, works_at → Acme Corp
        """
        conn = self._conn()

        rows = conn.execute("""
            SELECT e.id, e.name, e.type, COUNT(t.id) as cnt
            FROM entities e
            LEFT JOIN triples t ON t.subject = e.id AND t.valid_to IS NULL
            GROUP BY e.id
            HAVING cnt > 0
            ORDER BY cnt DESC
            LIMIT ?
        """, (max_entities,)).fetchall()

        if not rows:
            conn.close()
            return ""

        lines = []
        for eid, name, etype, _ in rows:
            triples = conn.execute(
                """SELECT t.predicate, COALESCE(e.name, t.object) as obj_display
                   FROM triples t
                   LEFT JOIN entities e ON t.object = e.id
                   WHERE t.subject = ? AND t.valid_to IS NULL
                   ORDER BY t.predicate""",
                (eid,),
            ).fetchall()
            if not triples:
                continue

            parts = []
            for pred, obj in triples:
                if pred in VALUE_PREDICATES:
                    parts.append(f"{pred}={obj}")
                else:
                    parts.append(f"{pred} → {obj}")
            lines.append(f"- {name} ({etype}): {', '.join(parts)}")

        conn.close()
        return "\n".join(lines)
