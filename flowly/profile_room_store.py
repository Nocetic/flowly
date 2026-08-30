"""Transactional SQLite persistence for host-owned profile rooms."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

SQLITE_SCHEMA_VERSION = 3
SQLITE_SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3)
SQLITE_APPLICATION_ID = 0x464C5952  # ``FLYR`` — Flowly room store.
MAX_SQLITE_STORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ROOM_METADATA_BYTES = 256 * 1024
MAX_MESSAGE_RECORD_BYTES = 4 * 1024 * 1024
MIGRATION_LOCK_TIMEOUT_SECONDS = 30.0


class RoomStoreError(RuntimeError):
    """Base error for a room database that cannot be trusted or updated."""


class RoomStoreInvalidError(RoomStoreError):
    """Raised when an existing database fails schema or integrity checks."""


class RoomStoreConflictError(RoomStoreError):
    """Raised when another process committed after this service loaded."""


class RoomStoreLimitError(RoomStoreError):
    """Raised when a record or database exceeds its defensive size bound."""


@dataclass(frozen=True)
class LoadedRoomStore:
    rooms: list[dict[str, Any]]
    revision: int


def canonical_room_json(room: dict[str, Any]) -> str:
    return json.dumps(
        room,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def assign_message_sequences(room: dict[str, Any]) -> None:
    """Give every message in ``room`` a sequence number, in place.

    Used when adopting a snapshot that predates sequencing (a legacy JSON
    store, or an imported room). Position is the only ordering information
    such a snapshot carries, so it becomes the initial sequence; from then on
    sequences are only ever handed out by ``next_room_sequence`` and are never
    reused, which is what lets a pagination cursor name one.
    """
    messages = room.get("messages") or []
    for ordinal, message in enumerate(messages):
        message["seq"] = ordinal
    room["nextSeq"] = len(messages)
    room["trimmedCount"] = 0


def ensure_message_sequences(room: dict[str, Any]) -> None:
    """Backfill sequences for any message that reached the room without one.

    Rooms enter memory from several places — a durable load, a preserved
    legacy snapshot, an import — and only the durable load carries sequences.
    Rather than trusting every one of those paths, this runs once at the point
    where a room becomes durable, so nothing can be written without the
    coordinate that history and cursors depend on.
    """
    messages = room.get("messages") or []
    highest = max(
        (
            int(message["seq"])
            for message in messages
            if isinstance(message.get("seq"), int) and not isinstance(message.get("seq"), bool)
        ),
        default=-1,
    )
    counter = max(int(room.get("nextSeq", 0)), highest + 1)
    for message in messages:
        seq = message.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            continue
        message["seq"] = counter
        counter += 1
    room["nextSeq"] = max(counter, int(room.get("nextSeq", 0)))
    room.setdefault("trimmedCount", 0)


def next_room_sequence(room: dict[str, Any]) -> int:
    """Reserve the next never-used sequence number for ``room``."""
    messages = room.get("messages") or []
    highest = max((int(message.get("seq", -1)) for message in messages), default=-1)
    seq = max(int(room.get("nextSeq", 0)), highest + 1)
    room["nextSeq"] = seq + 1
    return seq


def _room_next_seq(room: dict[str, Any], messages: list[dict[str, Any]]) -> int:
    highest = max((int(message.get("seq", -1)) for message in messages), default=-1)
    return max(int(room.get("nextSeq", 0)), highest + 1, 0)


def room_fingerprints(rooms: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        room_id: hashlib.sha256(canonical_room_json(room).encode("utf-8")).hexdigest()
        for room_id, room in rooms.items()
    }


class SQLiteRoomStore:
    """Small synchronous store invoked through ``asyncio.to_thread``.

    Connections are deliberately short-lived. This avoids crossing threads
    with sqlite connection objects and lets WAL coordinate independent Desktop
    processes. A monotonic store revision detects stale writers instead of
    silently replacing changes loaded by another process.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.migration.lock")

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> LoadedRoomStore:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self._migrate_if_needed()
        return self._load_path(self.path, journal_mode="WAL")

    def history_page(
        self,
        room_id: str,
        *,
        before_seq: int | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int | None, bool, int]:
        """Read one page of durable history, oldest-first within the page.

        Keyset paging on ``(room_id, seq)`` — the table's primary key — so a
        page seek costs an index descent rather than a scan of everything
        newer than the cursor. Returns
        ``(messages, next_before_seq, has_more, total_count)``.
        """
        if limit < 1:
            raise ValueError("history limit must be positive")
        connection = self._connect(self.path, journal_mode="WAL")
        try:
            if before_seq is None:
                rows = connection.execute(
                    """
                    SELECT seq, payload_json FROM room_messages
                    WHERE room_id = ?
                    ORDER BY seq DESC LIMIT ?
                    """,
                    (room_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT seq, payload_json FROM room_messages
                    WHERE room_id = ? AND seq < ?
                    ORDER BY seq DESC LIMIT ?
                    """,
                    (room_id, before_seq, limit),
                ).fetchall()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM room_messages WHERE room_id = ?",
                    (room_id,),
                ).fetchone()[0]
            )
            messages: list[dict[str, Any]] = []
            for seq, payload_json in reversed(rows):
                if len(payload_json.encode("utf-8")) > MAX_MESSAGE_RECORD_BYTES:
                    raise RoomStoreLimitError(
                        "A group message record exceeds its safe size limit."
                    )
                payload = json.loads(payload_json)
                if not isinstance(payload, dict):
                    raise RoomStoreInvalidError("A group message record is invalid.")
                payload["seq"] = int(seq)
                messages.append(payload)
            if not messages:
                return [], None, False, total
            oldest = int(messages[0]["seq"])
            has_more = (
                connection.execute(
                    "SELECT 1 FROM room_messages WHERE room_id = ? AND seq < ? LIMIT 1",
                    (room_id, oldest),
                ).fetchone()
                is not None
            )
            return messages, (oldest if has_more else None), has_more, total
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RoomStoreInvalidError("The group database contains invalid records.") from exc
        except sqlite3.DatabaseError as exc:
            raise RoomStoreInvalidError("The group history could not be read safely.") from exc
        finally:
            connection.close()

    def _migrate_if_needed(self) -> None:
        """Bring an older database up to the current schema, one step at a time.

        Steps are applied in order from whatever version is on disk, so a
        database that skipped a release still arrives here correctly. The
        alternative — one branch per source version — needs a new branch for
        every pair and gets one of them wrong eventually.

        v1 → v2 gave messages durable sequences. v1 numbered them by position
        and re-derived every number on each write, so the numbers were only
        meaningful inside one snapshot; they are still a correct total order
        for the snapshot being migrated, which is what makes them usable as
        the initial sequences.

        v2 → v3 added the per-room usage meter. Nothing reads it before it is
        written, so the column arrives null and every room simply starts
        counting from its next turn.
        """
        connection = self._connect(self.path, journal_mode="WAL")
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        finally:
            connection.close()
        if version == SQLITE_SCHEMA_VERSION:
            return
        if application_id != SQLITE_APPLICATION_ID or version not in SQLITE_SUPPORTED_SCHEMA_VERSIONS:
            # Leave it alone; the load below reports the unrecognised schema.
            return
        with self._migration_lock():
            connection = self._connect(self.path, journal_mode="WAL")
            try:
                # Re-read under the lock: another process may have finished
                # the whole chain while this one waited for it.
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version >= SQLITE_SCHEMA_VERSION:
                    return
                connection.execute("PRAGMA foreign_keys = OFF")
                # Each step stamps its own version before the next begins.
                # ``executescript`` commits whatever is open before it runs, so
                # a chain cannot be one transaction however it is written — and
                # a crash between two steps must not leave finished work behind
                # an unchanged version number, where the next attempt would
                # replay it and fail on a column that already exists.
                for target, upgrade in (
                    (2, self._upgrade_to_v2), (3, self._upgrade_to_v3),
                ):
                    if version < target:
                        upgrade(connection)
                        connection.execute(f"PRAGMA user_version = {target}")
                connection.commit()
            except sqlite3.Error as exc:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise RoomStoreError(
                    "The group database could not be upgraded to the sequenced schema."
                ) from exc
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.close()

    @staticmethod
    def _upgrade_to_v3(connection: sqlite3.Connection) -> None:
        """Add the per-room usage meter. Null until the room's next turn."""
        connection.execute("ALTER TABLE rooms ADD COLUMN usage_json TEXT")

    @staticmethod
    def _upgrade_to_v2(connection: sqlite3.Connection) -> None:
        """Give messages sequences that outlive the snapshot they came from."""
        connection.executescript(
            """
            ALTER TABLE rooms ADD COLUMN next_seq INTEGER NOT NULL DEFAULT 0;
            CREATE TABLE room_messages_v2 (
                room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                trimmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, seq),
                UNIQUE (room_id, message_id)
            ) WITHOUT ROWID;
            INSERT INTO room_messages_v2(
                room_id, seq, message_id, payload_json, trimmed
            )
            SELECT room_id, ordinal, message_id, payload_json, 0
            FROM room_messages;
            DROP TABLE room_messages;
            ALTER TABLE room_messages_v2 RENAME TO room_messages;
            CREATE INDEX room_messages_id_idx ON room_messages(message_id);
            CREATE INDEX room_messages_live_idx
                ON room_messages(room_id, trimmed, seq);
            UPDATE rooms SET next_seq = COALESCE(
                (SELECT MAX(seq) + 1 FROM room_messages
                 WHERE room_messages.room_id = rooms.id), 0
            );
            """
        )

    def initialize_verified(
        self,
        rooms: dict[str, dict[str, Any]],
        *,
        legacy_digest: str = "",
    ) -> bool:
        """Publish a fully verified database without overwriting a winner.

        Returns ``False`` when another process initialized the authoritative
        database while this process waited for the migration lock.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._migration_lock():
            try:
                self.lock_path.chmod(0o600)
            except OSError:
                pass
            if self.path.exists():
                return False
            fd, raw_temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".migrating",
                dir=self.path.parent,
            )
            os.close(fd)
            temporary = Path(raw_temporary)
            temporary.unlink(missing_ok=True)
            try:
                revision = 1 if rooms else 0
                connection = self._connect(temporary, journal_mode="DELETE")
                try:
                    self._create_schema(connection)
                    connection.execute("BEGIN IMMEDIATE")
                    self._replace_all(connection, rooms, revision=revision)
                    connection.execute(
                        "INSERT OR REPLACE INTO room_store_meta(key, value) VALUES(?, ?)",
                        ("revision", str(revision)),
                    )
                    if legacy_digest:
                        connection.execute(
                            "INSERT OR REPLACE INTO room_store_meta(key, value) VALUES(?, ?)",
                            ("legacy_sha256", legacy_digest),
                        )
                    connection.commit()
                finally:
                    connection.close()

                loaded = self._load_path(temporary, journal_mode="DELETE")
                expected = {
                    room_id: canonical_room_json(room)
                    for room_id, room in rooms.items()
                }
                actual = {
                    str(room["id"]): canonical_room_json(room)
                    for room in loaded.rooms
                }
                if loaded.revision != revision or actual != expected:
                    raise RoomStoreInvalidError(
                        "The staged room database did not pass round-trip verification."
                    )
                # Prove WAL support before publication. The connection is
                # closed and checkpointed while the database is still staged,
                # so publishing never depends on unpaired sidecar files.
                connection = self._connect(temporary, journal_mode="WAL")
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    connection.close()
                wal_verified = self._load_path(temporary, journal_mode="WAL")
                if wal_verified.revision != revision:
                    raise RoomStoreInvalidError(
                        "The staged WAL room database failed verification."
                    )
                self._cleanup_sqlite_sidecars(temporary)
                self._fsync_file(temporary)
                temporary.chmod(0o600)
                os.replace(temporary, self.path)
                self.path.chmod(0o600)
                self._fsync_parent(self.path.parent)
                return True
            finally:
                self._cleanup_sqlite_files(temporary)

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        try:
            with FileLock(
                str(self.lock_path),
                timeout=MIGRATION_LOCK_TIMEOUT_SECONDS,
            ):
                yield
        except FileLockTimeout as exc:
            raise RoomStoreError(
                "Timed out waiting for another group database migration."
            ) from exc

    def apply(
        self,
        *,
        changed_rooms: list[dict[str, Any]],
        deleted_room_ids: list[str],
        expected_revision: int,
    ) -> int:
        if not changed_rooms and not deleted_room_ids:
            return expected_revision
        connection = self._connect(self.path, journal_mode="WAL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read_revision(connection)
            if current != expected_revision:
                connection.rollback()
                raise RoomStoreConflictError(
                    "The group database changed in another Flowly process."
                )
            next_revision = current + 1
            for room_id in deleted_room_ids:
                connection.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
            for room in changed_rooms:
                self._upsert_room(connection, room, row_revision=next_revision)
            connection.execute(
                "INSERT OR REPLACE INTO room_store_meta(key, value) VALUES(?, ?)",
                ("revision", str(next_revision)),
            )
            connection.commit()
            return next_revision
        except (RoomStoreConflictError, RoomStoreLimitError):
            raise
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            # Say which kind of refusal this was. A contended database is a
            # retry; a violated invariant is a bug that must be reported as
            # one. Collapsing both into a single sentence hid a constraint
            # violation behind a message that reads like transient trouble.
            raise RoomStoreError(
                f"The group database could not commit a transaction "
                f"({type(exc).__name__}: {exc})."
            ) from exc
        finally:
            connection.close()

    def backup_to(self, destination: Path) -> None:
        """Create a transactionally consistent, single-file SQLite backup."""
        if destination.resolve(strict=False) == self.path.resolve(strict=False):
            raise ValueError("Room database backup destination must be different.")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.unlink(missing_ok=True)
        source = self._connect(self.path, journal_mode="WAL")
        target = self._connect(destination, journal_mode="DELETE")
        try:
            integrity = source.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RoomStoreInvalidError(
                    "The group database failed its pre-backup integrity check."
                )
            source.backup(target)
            target.commit()
        except sqlite3.Error as exc:
            raise RoomStoreError("The group database backup failed.") from exc
        finally:
            target.close()
            source.close()
        try:
            self._load_path(destination, journal_mode="DELETE")
            destination.chmod(0o600)
            self._fsync_file(destination)
        except BaseException:
            self._cleanup_sqlite_files(destination)
            raise

    def _load_path(self, path: Path, *, journal_mode: str) -> LoadedRoomStore:
        try:
            if path.stat().st_size > MAX_SQLITE_STORE_BYTES:
                raise RoomStoreLimitError("The group database exceeds its safe size limit.")
        except OSError as exc:
            raise RoomStoreError("The group database could not be inspected.") from exc
        connection = self._connect(path, journal_mode=journal_mode)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RoomStoreInvalidError("The group database failed its integrity check.")
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if (
                application_id != SQLITE_APPLICATION_ID
                or schema_version != SQLITE_SCHEMA_VERSION
            ):
                raise RoomStoreInvalidError("The group database schema is not recognized.")
            revision = self._read_revision(connection)
            rows = connection.execute(
                """
                SELECT id, title, mode, members_json, watermarks_json, run_json,
                       created_at, updated_at, next_seq, usage_json
                FROM rooms
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
            if len(rows) > 200:
                raise RoomStoreLimitError("The group database contains too many groups.")
            rooms: list[dict[str, Any]] = []
            for row in rows:
                metadata_size = sum(
                    len(str(value).encode("utf-8"))
                    for value in (row[3], row[4], row[5] or "")
                )
                if metadata_size > MAX_ROOM_METADATA_BYTES:
                    raise RoomStoreLimitError("A group record exceeds its safe size limit.")
                # Only the live window is resident. Trimmed rows stay on disk
                # and are reached by paging, so startup cost no longer grows
                # with a group's lifetime history.
                messages = connection.execute(
                    """
                    SELECT seq, payload_json
                    FROM room_messages
                    WHERE room_id = ? AND trimmed = 0
                    ORDER BY seq ASC
                    """,
                    (row[0],),
                ).fetchall()
                if len(messages) > 1_000:
                    raise RoomStoreLimitError("A group contains too many messages.")
                payloads: list[dict[str, Any]] = []
                for seq, payload_json in messages:
                    if len(payload_json.encode("utf-8")) > MAX_MESSAGE_RECORD_BYTES:
                        raise RoomStoreLimitError(
                            "A group message record exceeds its safe size limit."
                        )
                    payload = json.loads(payload_json)
                    if not isinstance(payload, dict):
                        raise RoomStoreInvalidError("A group message record is invalid.")
                    payload["seq"] = int(seq)
                    payloads.append(payload)
                trimmed_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM room_messages "
                        "WHERE room_id = ? AND trimmed = 1",
                        (row[0],),
                    ).fetchone()[0]
                )
                room: dict[str, Any] = {
                    "id": row[0],
                    "title": row[1],
                    "mode": row[2],
                    "members": json.loads(row[3]),
                    "watermarks": json.loads(row[4]),
                    "messages": payloads,
                    "createdAt": row[6],
                    "updatedAt": row[7],
                    "nextSeq": int(row[8]),
                    "trimmedCount": trimmed_count,
                }
                if row[5] is not None:
                    room["run"] = json.loads(row[5])
                if row[9] is not None:
                    room["usage"] = json.loads(row[9])
                rooms.append(room)
            return LoadedRoomStore(rooms=rooms, revision=revision)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RoomStoreInvalidError("The group database contains invalid records.") from exc
        except sqlite3.DatabaseError as exc:
            raise RoomStoreInvalidError("The group database could not be read safely.") from exc
        finally:
            connection.close()

    @staticmethod
    def _connect(path: Path, *, journal_mode: str) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            selected = str(
                connection.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()[0]
            ).upper()
            if selected != journal_mode.upper():
                connection.close()
                raise RoomStoreError(
                    f"The group database could not enable {journal_mode} journaling."
                )
            if journal_mode.upper() == "WAL":
                connection.execute("PRAGMA wal_autocheckpoint = 1000")
            return connection
        except sqlite3.Error as exc:
            raise RoomStoreInvalidError("The group database could not be opened safely.") from exc

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            PRAGMA application_id = {SQLITE_APPLICATION_ID};
            PRAGMA user_version = {SQLITE_SCHEMA_VERSION};
            CREATE TABLE room_store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE rooms (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL,
                members_json TEXT NOT NULL,
                watermarks_json TEXT NOT NULL,
                run_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                row_revision INTEGER NOT NULL,
                next_seq INTEGER NOT NULL DEFAULT 0,
                usage_json TEXT
            ) WITHOUT ROWID;
            CREATE TABLE room_messages (
                room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                trimmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, seq),
                UNIQUE (room_id, message_id)
            ) WITHOUT ROWID;
            CREATE INDEX room_messages_id_idx
                ON room_messages(message_id);
            CREATE INDEX room_messages_live_idx
                ON room_messages(room_id, trimmed, seq);
            INSERT INTO room_store_meta(key, value) VALUES('revision', '0');
            """
        )

    def _replace_all(
        self,
        connection: sqlite3.Connection,
        rooms: dict[str, dict[str, Any]],
        *,
        revision: int,
    ) -> None:
        connection.execute("DELETE FROM rooms")
        for room in rooms.values():
            # Staging a store from a legacy snapshot is the one place where
            # sequence numbers do not exist yet. Assign them from position and
            # write them back onto the caller's dicts: the round-trip
            # verification below compares against these very objects, and the
            # service adopts them as its live window.
            assign_message_sequences(room)
            self._upsert_room(connection, room, row_revision=revision)

    def _upsert_room(
        self,
        connection: sqlite3.Connection,
        room: dict[str, Any],
        *,
        row_revision: int,
    ) -> None:
        members_json = self._json(room.get("members", []))
        watermarks_json = self._json(room.get("watermarks", {}))
        run = room.get("run")
        run_json = self._json(run) if isinstance(run, dict) else None
        metadata_size = sum(
            len(value.encode("utf-8"))
            for value in (members_json, watermarks_json, run_json or "")
        )
        if metadata_size > MAX_ROOM_METADATA_BYTES:
            raise RoomStoreLimitError("A group record exceeds its safe size limit.")
        room_id = str(room["id"])
        usage = room.get("usage")
        usage_json = self._json(usage) if isinstance(usage, dict) else None
        messages = list(room.get("messages", []))
        next_seq = _room_next_seq(room, messages)
        connection.execute(
            """
            INSERT INTO rooms(
                id, title, mode, members_json, watermarks_json, run_json,
                created_at, updated_at, row_revision, next_seq, usage_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                mode = excluded.mode,
                members_json = excluded.members_json,
                watermarks_json = excluded.watermarks_json,
                run_json = excluded.run_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                row_revision = excluded.row_revision,
                next_seq = MAX(rooms.next_seq, excluded.next_seq),
                usage_json = excluded.usage_json
            """,
            (
                room_id,
                str(room["title"]),
                str(room.get("mode") or "panel"),
                members_json,
                watermarks_json,
                run_json,
                str(room["createdAt"]),
                str(room["updatedAt"]),
                row_revision,
                next_seq,
                usage_json,
            ),
        )
        # Only the live window is rewritten, and only where it actually
        # differs. The previous implementation deleted and re-inserted every
        # message on every turn, which made a single reply cost one write per
        # message in the room and forced sequence numbers to be re-derived
        # from position — the reason a cursor could never be a sequence.
        stored: dict[int, str] = {}
        stored_seq_by_id: dict[str, int] = {}
        for seq, message_id, payload_json in connection.execute(
            "SELECT seq, message_id, payload_json FROM room_messages "
            "WHERE room_id = ? AND trimmed = 0",
            (room_id,),
        ):
            stored[int(seq)] = str(payload_json)
            stored_seq_by_id[str(message_id)] = int(seq)
        records: list[tuple[str, int, str, str]] = []
        live_seqs: set[int] = set()
        for message in messages:
            seq = int(message["seq"])
            live_seqs.add(seq)
            payload = self._json(message)
            if len(payload.encode("utf-8")) > MAX_MESSAGE_RECORD_BYTES:
                raise RoomStoreLimitError("A group message record exceeds its safe size limit.")
            if stored.get(seq) != payload:
                records.append((room_id, seq, str(message["id"]), payload))
        # A message that arrives under a different sequence than the one it
        # is stored at has MOVED, and the row it left behind is its old
        # coordinate — not a second copy of the message. Retiring that row
        # first is what makes the write idempotent: without it the upsert
        # re-keyed one row into another's identity and tripped
        # ``UNIQUE (room_id, message_id)``, which failed the whole
        # transaction and, because rooms commit together, every unrelated
        # group in the same commit with it. Nothing moves on an ordinary
        # turn, so this repair costs no statement in the normal path.
        moved = [
            (room_id, message_id, seq)
            for _room, seq, message_id, _payload in records
            if stored_seq_by_id.get(message_id, seq) != seq
        ]
        if moved:
            connection.executemany(
                "DELETE FROM room_messages "
                "WHERE room_id = ? AND message_id = ? AND seq != ?",
                moved,
            )
        connection.executemany(
            """
            INSERT INTO room_messages(room_id, seq, message_id, payload_json, trimmed)
            VALUES(?, ?, ?, ?, 0)
            ON CONFLICT(room_id, seq) DO UPDATE SET
                message_id = excluded.message_id,
                payload_json = excluded.payload_json,
                trimmed = 0
            """,
            records,
        )
        # Rows that left the live window are flagged, never deleted. History
        # pages straight back into them, so a long-running group no longer
        # loses its opening turns the moment it crosses the window size.
        evicted = [(room_id, seq) for seq in stored if seq not in live_seqs]
        connection.executemany(
            "UPDATE room_messages SET trimmed = 1 WHERE room_id = ? AND seq = ?",
            evicted,
        )

    @staticmethod
    def _read_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM room_store_meta WHERE key = 'revision'"
        ).fetchone()
        if row is None:
            raise RoomStoreInvalidError("The group database revision is missing.")
        try:
            revision = int(row[0])
        except (TypeError, ValueError) as exc:
            raise RoomStoreInvalidError("The group database revision is invalid.") from exc
        if revision < 0:
            raise RoomStoreInvalidError("The group database revision is invalid.")
        return revision

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _cleanup_sqlite_files(path: Path) -> None:
        SQLiteRoomStore._cleanup_sqlite_sidecars(path)
        path.unlink(missing_ok=True)

    @staticmethod
    def _cleanup_sqlite_sidecars(path: Path) -> None:
        for candidate in (
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
            path.with_name(f"{path.name}-journal"),
        ):
            candidate.unlink(missing_ok=True)
