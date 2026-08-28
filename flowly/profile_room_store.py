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

SQLITE_SCHEMA_VERSION = 1
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
        return self._load_path(self.path, journal_mode="WAL")

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
            raise RoomStoreError("The group database could not commit a transaction.") from exc
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
                       created_at, updated_at
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
                messages = connection.execute(
                    """
                    SELECT payload_json
                    FROM room_messages
                    WHERE room_id = ?
                    ORDER BY ordinal ASC
                    """,
                    (row[0],),
                ).fetchall()
                if len(messages) > 1_000:
                    raise RoomStoreLimitError("A group contains too many messages.")
                payloads: list[dict[str, Any]] = []
                for (payload_json,) in messages:
                    if len(payload_json.encode("utf-8")) > MAX_MESSAGE_RECORD_BYTES:
                        raise RoomStoreLimitError(
                            "A group message record exceeds its safe size limit."
                        )
                    payload = json.loads(payload_json)
                    if not isinstance(payload, dict):
                        raise RoomStoreInvalidError("A group message record is invalid.")
                    payloads.append(payload)
                room: dict[str, Any] = {
                    "id": row[0],
                    "title": row[1],
                    "mode": row[2],
                    "members": json.loads(row[3]),
                    "watermarks": json.loads(row[4]),
                    "messages": payloads,
                    "createdAt": row[6],
                    "updatedAt": row[7],
                }
                if row[5] is not None:
                    room["run"] = json.loads(row[5])
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
                row_revision INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE room_messages (
                room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (room_id, ordinal),
                UNIQUE (room_id, message_id)
            ) WITHOUT ROWID;
            CREATE INDEX room_messages_id_idx
                ON room_messages(message_id);
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
        connection.execute(
            """
            INSERT INTO rooms(
                id, title, mode, members_json, watermarks_json, run_json,
                created_at, updated_at, row_revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                mode = excluded.mode,
                members_json = excluded.members_json,
                watermarks_json = excluded.watermarks_json,
                run_json = excluded.run_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                row_revision = excluded.row_revision
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
            ),
        )
        connection.execute("DELETE FROM room_messages WHERE room_id = ?", (room_id,))
        records: list[tuple[str, int, str, str]] = []
        for ordinal, message in enumerate(room.get("messages", [])):
            payload = self._json(message)
            if len(payload.encode("utf-8")) > MAX_MESSAGE_RECORD_BYTES:
                raise RoomStoreLimitError("A group message record exceeds its safe size limit.")
            records.append((room_id, ordinal, str(message["id"]), payload))
        connection.executemany(
            """
            INSERT INTO room_messages(room_id, ordinal, message_id, payload_json)
            VALUES(?, ?, ?, ?)
            """,
            records,
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
