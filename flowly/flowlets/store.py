"""SQLite-backed flowlet store: definitions (versioned) + state + event log.

Deliberately separate from the artifact store — a flowlet is state + an event
log, not a versioned document, so it gets its own schema and its own
``~/.flowly/flowlets.sqlite`` file. Single-writer via an ``RLock`` (the agent
tool and the ``flowlets.action`` RPC both write); timestamps are epoch
milliseconds to match the board/cron UI convention the clients already speak.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.flowlets import catalog

#: An attachment id is server-minted (`att_<hex>`); validated on every read/delete
#: so a client-supplied field value can never escape the flowlet's own dir.
_SAFE_ATTACH_ID = re.compile(r"^att_[0-9a-f]{8,}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flowlets (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    icon           TEXT,
    accent         TEXT,
    definition     TEXT NOT NULL DEFAULT '{}',
    catalog        INTEGER NOT NULL DEFAULT 1,
    version        INTEGER NOT NULL DEFAULT 1,
    pinned         INTEGER NOT NULL DEFAULT 0,
    origin_session TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flowlets_updated ON flowlets(updated_at DESC);

CREATE TABLE IF NOT EXISTS flowlet_versions (
    id          TEXT PRIMARY KEY,
    flowlet_id  TEXT NOT NULL REFERENCES flowlets(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    definition  TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flowlet_versions
    ON flowlet_versions(flowlet_id, version DESC);

CREATE TABLE IF NOT EXISTS flowlet_state (
    flowlet_id  TEXT NOT NULL REFERENCES flowlets(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (flowlet_id, key)
);

CREATE TABLE IF NOT EXISTS flowlet_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flowlet_id  TEXT NOT NULL REFERENCES flowlets(id) ON DELETE CASCADE,
    series      TEXT NOT NULL,
    value       REAL NOT NULL DEFAULT 0,
    meta        TEXT,
    ts          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flowlet_events
    ON flowlet_events(flowlet_id, series, ts);

CREATE TABLE IF NOT EXISTS flowlet_watch_state (
    flowlet_id    TEXT NOT NULL REFERENCES flowlets(id) ON DELETE CASCADE,
    watch_id      TEXT NOT NULL,
    last_fired_ms INTEGER,
    last_cond     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flowlet_id, watch_id)
);

CREATE TABLE IF NOT EXISTS flowlet_source_state (
    flowlet_id   TEXT NOT NULL REFERENCES flowlets(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    last_ok_ms   INTEGER,
    last_err_ms  INTEGER,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    PRIMARY KEY (flowlet_id, source_id)
);
"""

_SCHEMA_VERSION = "3"


def now_ms() -> int:
    return int(time.time() * 1000)


def _gen_id(prefix: str = "flt") -> str:
    ts = int(time.time()).to_bytes(4, "big").hex()
    rand = os.urandom(4).hex()
    return f"{prefix}_{ts}_{rand}"


def _parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return fallback
    return value if value is not None else fallback


# ── Singleton per state dir ───────────────────────────────────────────────────

_CACHE: dict[str, "FlowletStore"] = {}


def get_store(state_dir: Path | None = None) -> "FlowletStore":
    """Return the FlowletStore for ``state_dir``.

    The default resolves through :func:`flowly.profile.get_flowly_home` (the
    single source of truth, FLOWLY_HOME- and profile-aware) so the agent tool
    (which passes ``state_dir=get_data_dir()``) and the feature_rpc handlers
    (which call this with no arg) always land on the SAME store — otherwise a
    profile/FLOWLY_HOME run would silently split them across two DB files.
    """
    if state_dir is None:
        from flowly.profile import get_flowly_home
        state_dir = get_flowly_home()
    key = str(state_dir)
    if key not in _CACHE:
        _CACHE[key] = FlowletStore(state_dir / "flowlets.sqlite")
    return _CACHE[key]


class FlowletStore:
    """Single-writer SQLite persistence for flowlets."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
                )
            self._conn.commit()

    # ── Flowlet CRUD ──────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        definition: dict,
        icon: str | None = None,
        accent: str | None = None,
        catalog: int = 1,
        pinned: bool = False,
        origin_session: str | None = None,
    ) -> dict:
        flowlet_id = _gen_id()
        ts = now_ms()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO flowlets
                   (id, name, icon, accent, definition, catalog, version,
                    pinned, origin_session, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (flowlet_id, name, icon, accent, json.dumps(definition), catalog,
                 1 if pinned else 0, origin_session, ts, ts),
            )
        logger.debug("Flowlet created: {} ({})", flowlet_id, name)
        return self.get(flowlet_id)  # type: ignore[return-value]

    def get(self, flowlet_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM flowlets WHERE id = ?", (flowlet_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update(
        self,
        flowlet_id: str,
        name: str | None = None,
        icon: str | None = None,
        accent: str | None = None,
        definition: dict | None = None,
        pinned: bool | None = None,
    ) -> dict | None:
        existing = self.get(flowlet_id)
        if not existing:
            return None
        ts = now_ms()
        with self._lock, self._conn:
            version_bump = False
            if definition is not None:
                new_json = json.dumps(definition)
                if new_json != json.dumps(existing["definition"]):
                    # snapshot the outgoing version before overwriting
                    self._conn.execute(
                        """INSERT INTO flowlet_versions
                           (id, flowlet_id, version, definition, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (_gen_id("fver"), flowlet_id, existing["version"],
                         json.dumps(existing["definition"]), ts),
                    )
                    version_bump = True

            sets = ["updated_at = ?"]
            params: list[Any] = [ts]
            if name is not None:
                sets.append("name = ?")
                params.append(name)
            if icon is not None:
                sets.append("icon = ?")
                params.append(icon)
            if accent is not None:
                sets.append("accent = ?")
                params.append(accent)
            if definition is not None:
                sets.append("definition = ?")
                params.append(json.dumps(definition))
                if "catalog" in definition:
                    sets.append("catalog = ?")
                    params.append(int(definition["catalog"]))
            if pinned is not None:
                sets.append("pinned = ?")
                params.append(1 if pinned else 0)
            if version_bump:
                sets.append("version = version + 1")
            params.append(flowlet_id)
            self._conn.execute(
                f"UPDATE flowlets SET {', '.join(sets)} WHERE id = ?", params
            )
        return self.get(flowlet_id)

    def delete(self, flowlet_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM flowlets WHERE id = ?", (flowlet_id,)
            )
            # ON DELETE CASCADE clears state/events/versions
            deleted = cur.rowcount > 0
        if deleted:
            self.delete_flowlet_attachments(flowlet_id)  # photos live on disk, not in the DB
        return deleted

    # ── Attachments (captured photos for `vision` / `image` item fields) ──────
    # Stored as one JPEG per capture under the DB's own dir; the flowlet DB holds
    # only the id. Kept out of the DB (and out of state broadcasts) so images
    # never bloat sync or the row payload.

    def _attach_dir(self, flowlet_id: str) -> Path:
        return self._db_path.parent / "flowlet_attachments" / flowlet_id

    def put_attachment(self, flowlet_id: str, data: bytes) -> str:
        """Store one image and return its attachment id. Raises if the per-flowlet
        cap is reached (bounds disk use)."""
        d = self._attach_dir(flowlet_id)
        with self._lock:
            d.mkdir(parents=True, exist_ok=True)
            if sum(1 for _ in d.glob("*.jpg")) >= catalog.MAX_ATTACHMENTS_PER_FLOWLET:
                raise ValueError(
                    f"attachment limit ({catalog.MAX_ATTACHMENTS_PER_FLOWLET}) reached "
                    f"for flowlet {flowlet_id}"
                )
            att_id = f"att_{os.urandom(8).hex()}"
            (d / f"{att_id}.jpg").write_bytes(data)
        return att_id

    def get_attachment(self, flowlet_id: str, att_id: str) -> bytes | None:
        if not _SAFE_ATTACH_ID.match(att_id or ""):
            return None
        try:
            return (self._attach_dir(flowlet_id) / f"{att_id}.jpg").read_bytes()
        except OSError:
            return None

    def attachment_path(self, flowlet_id: str, att_id: str) -> Path | None:
        """The on-disk path of a stored photo — for the agent's media pipeline,
        which consumes local file paths (same contract as chat attachments)."""
        if not _SAFE_ATTACH_ID.match(att_id or ""):
            return None
        p = self._attach_dir(flowlet_id) / f"{att_id}.jpg"
        return p if p.is_file() else None

    def delete_attachment(self, flowlet_id: str, att_id: str) -> None:
        if not _SAFE_ATTACH_ID.match(att_id or ""):
            return
        try:
            (self._attach_dir(flowlet_id) / f"{att_id}.jpg").unlink(missing_ok=True)
        except OSError:
            pass

    def delete_flowlet_attachments(self, flowlet_id: str) -> None:
        shutil.rmtree(self._attach_dir(flowlet_id), ignore_errors=True)

    def pin(self, flowlet_id: str, pinned: bool = True) -> dict | None:
        return self.update(flowlet_id, pinned=pinned)

    def list(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM flowlets ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_versions(self, flowlet_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM flowlet_versions
                   WHERE flowlet_id = ? ORDER BY version DESC""",
                (flowlet_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["definition"] = _parse_json(d.get("definition"), {})
            out.append(d)
        return out

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, flowlet_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM flowlet_state WHERE flowlet_id = ?",
                (flowlet_id,),
            ).fetchall()
        return {r["key"]: _parse_json(r["value"], None) for r in rows}

    def set_state(self, flowlet_id: str, key: str, value: Any) -> None:
        ts = now_ms()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO flowlet_state (flowlet_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(flowlet_id, key)
                   DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (flowlet_id, key, json.dumps(value), ts),
            )

    def reset_state(self, flowlet_id: str, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM flowlet_state WHERE flowlet_id = ? AND key = ?",
                (flowlet_id, key),
            )

    # ── Events ────────────────────────────────────────────────────────────────

    def add_event(
        self,
        flowlet_id: str,
        series: str,
        value: float,
        meta: dict | None = None,
        ts: int | None = None,
    ) -> int:
        ts = ts if ts is not None else now_ms()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO flowlet_events (flowlet_id, series, value, meta, ts)
                   VALUES (?, ?, ?, ?, ?)""",
                (flowlet_id, series, float(value),
                 json.dumps(meta) if meta else None, ts),
            )
            return int(cur.lastrowid)

    def get_events(self, flowlet_id: str) -> list[dict]:
        """All events for a flowlet, sorted by ts ascending (so `last` works)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT series, value, ts, meta FROM flowlet_events
                   WHERE flowlet_id = ? ORDER BY ts ASC, id ASC""",
                (flowlet_id,),
            ).fetchall()
        return [
            {"series": r["series"], "value": r["value"], "ts": r["ts"],
             "meta": _parse_json(r["meta"], None)}
            for r in rows
        ]

    def remove_last_event(self, flowlet_id: str, series: str) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute(
                """SELECT id FROM flowlet_events
                   WHERE flowlet_id = ? AND series = ?
                   ORDER BY ts DESC, id DESC LIMIT 1""",
                (flowlet_id, series),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM flowlet_events WHERE id = ?", (row["id"],))
            return True

    def reset_events(self, flowlet_id: str, series: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM flowlet_events WHERE flowlet_id = ? AND series = ?",
                (flowlet_id, series),
            )
            return int(cur.rowcount)

    def last_activity_ms(self, flowlet_id: str) -> int | None:
        """Newest state-write or event timestamp for a flowlet, or ``None`` if it
        has neither yet. Backs ``stale`` watches ("no activity for N minutes")."""
        with self._lock:
            r1 = self._conn.execute(
                "SELECT MAX(updated_at) AS m FROM flowlet_state WHERE flowlet_id = ?",
                (flowlet_id,),
            ).fetchone()
            r2 = self._conn.execute(
                "SELECT MAX(ts) AS m FROM flowlet_events WHERE flowlet_id = ?",
                (flowlet_id,),
            ).fetchone()
        cands = [r["m"] for r in (r1, r2) if r and r["m"] is not None]
        return max(cands) if cands else None

    # ── Watch runtime state (edge detection + cooldown for reactive rules) ──────

    def get_watch_state(self, flowlet_id: str) -> dict[str, dict]:
        """``{watch_id: {"last_fired_ms": int|None, "last_cond": bool}}`` for a
        flowlet. Missing rows mean "never evaluated" (the engine treats absent as
        last_cond=False, last_fired=None)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT watch_id, last_fired_ms, last_cond
                   FROM flowlet_watch_state WHERE flowlet_id = ?""",
                (flowlet_id,),
            ).fetchall()
        return {
            r["watch_id"]: {
                "last_fired_ms": r["last_fired_ms"],
                "last_cond": bool(r["last_cond"]),
            }
            for r in rows
        }

    def set_watch_state(
        self,
        flowlet_id: str,
        watch_id: str,
        *,
        last_fired_ms: int | None = None,
        last_cond: bool | None = None,
    ) -> None:
        """Upsert a watch's runtime state, overwriting only the fields provided
        (a ``None`` argument leaves the stored value untouched)."""
        with self._lock, self._conn:
            row = self._conn.execute(
                """SELECT last_fired_ms, last_cond FROM flowlet_watch_state
                   WHERE flowlet_id = ? AND watch_id = ?""",
                (flowlet_id, watch_id),
            ).fetchone()
            cur_fired = row["last_fired_ms"] if row else None
            cur_cond = row["last_cond"] if row else 0
            new_fired = last_fired_ms if last_fired_ms is not None else cur_fired
            new_cond = (1 if last_cond else 0) if last_cond is not None else cur_cond
            self._conn.execute(
                """INSERT INTO flowlet_watch_state
                       (flowlet_id, watch_id, last_fired_ms, last_cond)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(flowlet_id, watch_id)
                   DO UPDATE SET last_fired_ms = excluded.last_fired_ms,
                                 last_cond    = excluded.last_cond""",
                (flowlet_id, watch_id, new_fired, new_cond),
            )

    # ── Source runtime state (refresh scheduling + backoff) ─────────────────────

    def get_source_state(self, flowlet_id: str) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT source_id, last_ok_ms, last_err_ms, fail_count, last_error
                   FROM flowlet_source_state WHERE flowlet_id = ?""",
                (flowlet_id,),
            ).fetchall()
        return {
            r["source_id"]: {
                "last_ok_ms": r["last_ok_ms"],
                "last_err_ms": r["last_err_ms"],
                "fail_count": r["fail_count"],
                "last_error": r["last_error"],
            }
            for r in rows
        }

    def set_source_state(
        self,
        flowlet_id: str,
        source_id: str,
        *,
        last_ok_ms: int | None = None,
        last_err_ms: int | None = None,
        fail_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        """Upsert a source's runtime state, overwriting only provided fields."""
        with self._lock, self._conn:
            row = self._conn.execute(
                """SELECT last_ok_ms, last_err_ms, fail_count, last_error
                   FROM flowlet_source_state WHERE flowlet_id = ? AND source_id = ?""",
                (flowlet_id, source_id),
            ).fetchone()
            cur = dict(row) if row else {
                "last_ok_ms": None, "last_err_ms": None, "fail_count": 0, "last_error": None,
            }
            new = {
                "last_ok_ms": last_ok_ms if last_ok_ms is not None else cur["last_ok_ms"],
                "last_err_ms": last_err_ms if last_err_ms is not None else cur["last_err_ms"],
                "fail_count": fail_count if fail_count is not None else cur["fail_count"],
                "last_error": last_error if last_error is not None else cur["last_error"],
            }
            self._conn.execute(
                """INSERT INTO flowlet_source_state
                       (flowlet_id, source_id, last_ok_ms, last_err_ms, fail_count, last_error)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(flowlet_id, source_id) DO UPDATE SET
                       last_ok_ms = excluded.last_ok_ms,
                       last_err_ms = excluded.last_err_ms,
                       fail_count = excluded.fail_count,
                       last_error = excluded.last_error""",
                (flowlet_id, source_id, new["last_ok_ms"], new["last_err_ms"],
                 new["fail_count"], new["last_error"]),
            )

    # ── Row conversion ────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["definition"] = _parse_json(d.get("definition"), {})
        d["pinned"] = bool(d.get("pinned", 0))
        return d
