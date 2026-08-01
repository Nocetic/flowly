"""The media library — a durable index over ``~/.flowly/media``.

Everything Flowly generates or is handed lands as a file in one flat directory.
Delivery has always worked off that directory (``mediaId`` *is* the basename;
see :mod:`flowly.media.serving`), but nothing ever *remembered* what any of it
was. A picture's provider, model, prompt and originating chat were measured at
generation time, written into one message's metadata, and then only reachable by
scrolling to that message. There was no answer to "show me everything my agent
has made", and no way to find ``img-9f3a71c2b804.png`` except to recognise it.

This module is that memory. Three decisions shape it.

**The index is a cache of the disk, not the other way round.** Files are the
truth: generation writes them, retention deletes them, and neither asks
permission. So every read path tolerates a row whose file is gone, and
:meth:`MediaLibrary.reconcile` — not a foreign key — is what keeps the two in
step. Losing ``media.sqlite`` costs provenance, never bytes; a rescan rebuilds a
working (if plainer) library from the directory alone.

**Only servable media is indexed.** ``SERVABLE_MIME_PREFIXES`` is image, video
and audio. A row for anything else would be a row no client could ever open, so
documents, ``model-catalog.json`` and other bookkeeping simply never enter. The
library shows what it can actually hand you.

**The database lives OUTSIDE the media directory.** ``prune_media`` treats every
regular file directly inside ``~/.flowly/media`` as a deletable unit, so a
``.sqlite`` file there would be classified as a stray ``file`` and evicted under
the image budget. The database sits at ``~/.flowly/media.sqlite``; the thumbnail
cache sits in a *subdirectory*, which retention documents itself as leaving
alone.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.media.assets import (
    KIND_AUDIO,
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_EXPIRED,
    STATUS_READY,
    MediaAsset,
    guess_mime,
    kind_for_mime,
)

# ── Vocabulary ────────────────────────────────────────────────────────────────

#: Made by a tool on this machine (image_generate, video_generate, voice, …).
SOURCE_GENERATED = "generated"
#: Handed to the agent by a person — a chat upload, an iMessage, a Telegram photo.
SOURCE_RECEIVED = "received"

SOURCES = frozenset({SOURCE_GENERATED, SOURCE_RECEIVED})

#: The kinds that can actually be served, and therefore the only kinds indexed.
#: Mirrors ``serving.SERVABLE_MIME_PREFIXES`` — a row outside this set would be
#: a library entry no client could open.
INDEXED_KINDS = frozenset({KIND_IMAGE, KIND_VIDEO, KIND_AUDIO})

#: Thumbnail cache, a SUBdirectory of the media dir so ``prune_media`` (which
#: only touches regular files directly inside it) leaves it alone. The library
#: owns its lifetime via :meth:`MediaLibrary.reconcile`.
THUMB_DIR_NAME = "thumbs"

#: Longest edge of a cached thumbnail. A grid tile is ~160 pt; 256 px covers it
#: at 1.5x and still encodes to roughly 10 KB, so a 50-item page inlines in
#: well under a megabyte — comfortably inside the relay's 10 MB frame.
THUMB_MAX_EDGE = 256
THUMB_QUALITY = 72

#: Thumbnail states. ``pending`` has never been tried; ``none`` means we tried
#: and this file cannot produce one (no ffmpeg for a clip, a corrupt image), and
#: exists so a hopeless file is not re-attempted on every single page render.
THUMB_PENDING = "pending"
THUMB_READY = "ready"
THUMB_NONE = "none"

#: Ceiling on thumbnails generated to satisfy one ``list()`` call. A cold
#: library must not turn the first page load into a hundred ffmpeg invocations;
#: uncovered rows come back thumbnail-less and are filled in on a later page.
MAX_THUMBS_PER_LIST = 24


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    media_id     TEXT PRIMARY KEY,
    asset_id     TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL,
    source       TEXT NOT NULL,
    mime_type    TEXT NOT NULL DEFAULT '',
    file_name    TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    width        INTEGER,
    height       INTEGER,
    duration_ms  INTEGER,
    poster_id    TEXT,
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    prompt       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    session_key  TEXT NOT NULL DEFAULT '',
    channel      TEXT NOT NULL DEFAULT '',
    message_ts   TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'ready',
    starred      INTEGER NOT NULL DEFAULT 0,
    thumb_state  TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_kind_created
    ON media(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_source_created
    ON media(source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_session
    ON media(session_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
CREATE INDEX IF NOT EXISTS idx_media_starred
    ON media(starred) WHERE starred = 1;

CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
    prompt, title, file_name, media_id UNINDEXED, tokenize = 'unicode61'
);
"""

_SCHEMA_VERSION = "1"

#: Set once the one-time walk of session transcripts has run. Backfill is
#: expensive on a long-lived install and pointless twice.
_BACKFILL_KEY = "sessions_backfilled"


# ── Helpers ───────────────────────────────────────────────────────────────────


def thumbs_dir(media_dir: Path) -> Path:
    return media_dir / THUMB_DIR_NAME


def _clean_str(value: Any, limit: int = 2000) -> str:
    """A trimmed, length-capped string. Anything unusable becomes ``''``."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _is_poster_for(image: Path, siblings: dict[str, list[Path]]) -> bool:
    """True when *image* is the poster frame of a same-stem clip or track.

    The exact relationship ``prune_media`` groups on (``retention.media_units``):
    an image sitting next to a video or audio file with the same stem is that
    file's preview, not a picture in its own right, and must not surface in the
    gallery as a duplicate tile.
    """
    for sibling in siblings.get(image.stem, ()):
        if sibling == image:
            continue
        if kind_for_mime(guess_mime(sibling)) in (KIND_VIDEO, KIND_AUDIO):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """One servable file found on disk, with its poster resolved."""

    path: Path
    kind: str
    mime_type: str
    size: int
    mtime: float
    poster: Path | None


def scan_media_dir(media_dir: Path) -> list[ScanEntry]:
    """Every servable file directly inside *media_dir*, posters folded in.

    Subdirectories are skipped (the thumbnail cache lives in one), as are
    dotfiles and anything whose mime is not image/video/audio — which is what
    keeps ``model-catalog.json`` and stray documents out of the library.
    """
    if not media_dir.is_dir():
        return []
    try:
        files = [p for p in media_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError as exc:
        logger.debug("[library] scan failed for {}: {}", media_dir, exc)
        return []

    by_stem: dict[str, list[Path]] = {}
    for path in files:
        by_stem.setdefault(path.stem, []).append(path)

    entries: list[ScanEntry] = []
    for path in files:
        mime = guess_mime(path)
        kind = kind_for_mime(mime)
        if kind not in INDEXED_KINDS:
            continue
        if kind == KIND_IMAGE and _is_poster_for(path, by_stem):
            continue
        poster: Path | None = None
        if kind in (KIND_VIDEO, KIND_AUDIO):
            for sibling in by_stem.get(path.stem, ()):
                if sibling != path and kind_for_mime(guess_mime(sibling)) == KIND_IMAGE:
                    poster = sibling
                    break
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            ScanEntry(
                path=path,
                kind=kind,
                mime_type=mime,
                size=int(stat.st_size),
                mtime=float(stat.st_mtime),
                poster=poster,
            )
        )
    return entries


# ── Singleton ─────────────────────────────────────────────────────────────────

_CACHE: dict[str, "MediaLibrary"] = {}
_CACHE_LOCK = threading.Lock()


def get_library(home: Path | None = None) -> "MediaLibrary":
    """The library for a profile home, created on first use.

    Cached per home so the sqlite connection is shared, matching how the
    artifact store is reached. Tests pass an explicit home.
    """
    if home is None:
        from flowly.profile import get_flowly_home

        home = get_flowly_home()
    key = str(home)
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = MediaLibrary(home / "media.sqlite", home / "media")
        return _CACHE[key]


# ── Change notification ───────────────────────────────────────────────────────
#
# The gallery has to update itself. Artifacts already solved this — a broadcast
# callback the gateway bootstrapper wires in — and copying that shape means the
# library needs no reference to a server it cannot see from a worker thread.

#: Event name every mutation publishes. One name rather than
#: created/updated/deleted, because a gallery re-reads a page either way and
#: three names would be three code paths on every client for one behaviour.
MEDIA_CHANGED_EVENT = "media.changed"

_ON_CHANGE: Any | None = None


def set_on_change(callback: Any | None) -> None:
    """Register the broadcast used to tell clients the library moved."""
    global _ON_CHANGE
    _ON_CHANGE = callback


async def notify_change(**data: Any) -> None:
    """Publish a library change. Never raises — this is a nicety, not a step."""
    if _ON_CHANGE is None:
        return
    try:
        await _ON_CHANGE(MEDIA_CHANGED_EVENT, data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[library] change broadcast failed: {}", exc)


async def record_async(
    assets: list[MediaAsset],
    *,
    source: str,
    session_key: str = "",
    channel: str = "",
    message_ts: str = "",
) -> int:
    """Index media from an async caller, off the event loop. Never raises.

    Every call site is on a path that is delivering a reply or accepting an
    upload. Indexing is bookkeeping: it runs in a worker thread so a slow disk
    cannot stall a turn, and a failure is logged and dropped rather than
    surfaced, because a missing library row is a smaller harm than a lost
    message.
    """
    if not assets:
        return 0
    try:
        import asyncio

        written = await asyncio.to_thread(
            get_library().record,
            list(assets),
            source=source,
            session_key=session_key,
            channel=channel,
            message_ts=message_ts,
        )
    except Exception as exc:  # noqa: BLE001 - indexing never breaks delivery
        logger.debug("[library] record_async failed: {}", exc)
        return 0
    if written:
        await notify_change(added=written, source=source)
    return written


async def record_paths_async(
    paths: list[str],
    *,
    source: str,
    session_key: str = "",
    channel: str = "",
    message_ts: str = "",
) -> int:
    """Index bare file paths — the inbound side, where nobody measured anything.

    Media a person sent arrives as a list of paths with no descriptor: no
    provider, no model, no prompt, and no dimensions. Describing each file here
    (in a worker thread, because it may shell out to ffprobe) is what lets an
    uploaded photo sit in the same grid as a generated one instead of as a
    dimensionless placeholder.

    Remote URLs and paths outside the media directory are dropped by
    :meth:`MediaLibrary.record`; they are not files this machine owns.
    """
    usable = [p for p in paths if isinstance(p, str) and p and not p.startswith(("http://", "https://"))]
    if not usable:
        return 0

    def _describe_and_record() -> int:
        from flowly.media.assets import describe

        assets = []
        for path in usable:
            try:
                if not Path(path).is_file():
                    continue
                assets.append(describe(path))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[library] describe failed for {}: {}", path, exc)
        return get_library().record(
            assets,
            source=source,
            session_key=session_key,
            channel=channel,
            message_ts=message_ts,
        )

    try:
        import asyncio

        written = await asyncio.to_thread(_describe_and_record)
    except Exception as exc:  # noqa: BLE001 - indexing never breaks delivery
        logger.debug("[library] record_paths_async failed: {}", exc)
        return 0
    if written:
        await notify_change(added=written, source=source)
    return written


async def expire_paths_async(paths: list[str]) -> int:
    """Mark rows for files retention just deleted. Never raises.

    Retention already knows exactly what it removed, so telling the library
    beats making it rediscover the fact by rescanning the directory.
    """
    names = [Path(p).name for p in paths if isinstance(p, str) and p]
    if not names:
        return 0
    try:
        import asyncio

        expired = await asyncio.to_thread(get_library().expire, names)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[library] expire_paths_async failed: {}", exc)
        return 0
    if expired:
        await notify_change(expired=expired)
    return expired


def reset_library_cache() -> None:
    """Drop every cached library. For tests and profile switches."""
    with _CACHE_LOCK:
        for library in _CACHE.values():
            library.close()
        _CACHE.clear()


# ── Store ─────────────────────────────────────────────────────────────────────


class MediaLibrary:
    """Durable index over one profile's media directory."""

    def __init__(self, db_path: Path, media_dir: Path):
        self._db_path = db_path
        self._media_dir = media_dir
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Writes arrive from worker threads (``record`` is called via
        # ``to_thread`` off the agent loop) while reads arrive from the RPC
        # dispatch. One lock is cheaper than reasoning about which is which.
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def media_dir(self) -> Path:
        return self._media_dir

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )

    # ── Meta ──────────────────────────────────────────────────────────────

    def _meta_get(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ── Write ─────────────────────────────────────────────────────────────

    def record(
        self,
        assets: list[MediaAsset],
        *,
        source: str,
        session_key: str = "",
        channel: str = "",
        message_ts: str = "",
    ) -> int:
        """Index media that just joined a conversation. Returns rows written.

        Best-effort by contract: this runs on the path that delivers a reply,
        and a library that failed to write must never cost the user their
        message. Every failure is logged at debug and swallowed.

        Assets whose file does not live directly inside the media directory are
        SKIPPED, not indexed. The desktop's local-attachment optimisation hands
        the gateway a path straight out of the user's Finder — Flowly never
        copied those bytes, cannot serve them by ``mediaId``, and must not
        pretend to own them.
        """
        if source not in SOURCES:
            source = SOURCE_GENERATED
        written = 0
        try:
            media_root = self._media_dir.resolve()
        except OSError:
            return 0

        for asset in assets:
            try:
                if asset.kind not in INDEXED_KINDS:
                    continue
                path = Path(asset.path)
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError):
                    continue
                if resolved.parent != media_root:
                    continue
                if not resolved.is_file():
                    continue
                poster_id = ""
                if asset.poster_path:
                    poster = Path(asset.poster_path)
                    if poster.is_file() and poster.resolve().parent == media_root:
                        poster_id = poster.name
                self._upsert(
                    media_id=resolved.name,
                    asset_id=asset.id,
                    kind=asset.kind,
                    source=source,
                    mime_type=asset.mime_type or guess_mime(resolved),
                    file_name=asset.file_name or resolved.name,
                    size=asset.size or resolved.stat().st_size,
                    width=asset.width,
                    height=asset.height,
                    duration_ms=asset.duration_ms,
                    poster_id=poster_id,
                    provider=asset.provider,
                    model=asset.model,
                    prompt=asset.prompt,
                    session_key=session_key,
                    channel=channel,
                    message_ts=message_ts,
                    created_at=time.time(),
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 - indexing never breaks a reply
                logger.debug("[library] record skipped {}: {}", asset.path, exc)
        return written

    def _upsert(
        self,
        *,
        media_id: str,
        asset_id: str,
        kind: str,
        source: str,
        mime_type: str,
        file_name: str,
        size: int,
        width: int | None,
        height: int | None,
        duration_ms: int | None,
        poster_id: str,
        provider: str,
        model: str,
        prompt: str,
        session_key: str,
        channel: str,
        message_ts: str,
        created_at: float,
    ) -> None:
        """Insert a row, or enrich the one a scan already created.

        ``COALESCE(NULLIF(excluded.x, ''), media.x)`` is the shape throughout:
        a later, better-informed write wins, but a write that knows *less* —
        typically a reconcile scan running after a real ``record`` — must not
        blank out provenance the generator supplied. ``created_at`` likewise
        only ever moves earlier, so a rescan cannot reshuffle the gallery.
        """
        now = time.time()
        title = _clean_str(prompt, 120) or file_name
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO media (
                       media_id, asset_id, kind, source, mime_type, file_name,
                       size, width, height, duration_ms, poster_id, provider,
                       model, prompt, title, session_key, channel, message_ts,
                       status, starred, thumb_state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, 0, ?, ?, ?)
                   ON CONFLICT(media_id) DO UPDATE SET
                       asset_id    = COALESCE(NULLIF(excluded.asset_id, ''), media.asset_id),
                       kind        = excluded.kind,
                       source      = excluded.source,
                       mime_type   = COALESCE(NULLIF(excluded.mime_type, ''), media.mime_type),
                       file_name   = COALESCE(NULLIF(excluded.file_name, ''), media.file_name),
                       size        = excluded.size,
                       width       = COALESCE(excluded.width, media.width),
                       height      = COALESCE(excluded.height, media.height),
                       duration_ms = COALESCE(excluded.duration_ms, media.duration_ms),
                       poster_id   = COALESCE(NULLIF(excluded.poster_id, ''), media.poster_id),
                       provider    = COALESCE(NULLIF(excluded.provider, ''), media.provider),
                       model       = COALESCE(NULLIF(excluded.model, ''), media.model),
                       prompt      = COALESCE(NULLIF(excluded.prompt, ''), media.prompt),
                       title       = COALESCE(NULLIF(excluded.prompt, ''), media.title),
                       session_key = COALESCE(NULLIF(excluded.session_key, ''), media.session_key),
                       channel     = COALESCE(NULLIF(excluded.channel, ''), media.channel),
                       message_ts  = COALESCE(NULLIF(excluded.message_ts, ''), media.message_ts),
                       status      = excluded.status,
                       created_at  = MIN(media.created_at, excluded.created_at),
                       updated_at  = excluded.updated_at""",
                (
                    media_id, asset_id, kind, source, mime_type, file_name,
                    max(0, int(size)), width, height, duration_ms, poster_id,
                    _clean_str(provider, 80), _clean_str(model, 200),
                    _clean_str(prompt), title, _clean_str(session_key, 200),
                    _clean_str(channel, 40), _clean_str(message_ts, 64),
                    STATUS_READY, THUMB_PENDING, created_at, now,
                ),
            )
            self._fts_sync(media_id)

    def _fts_sync(self, media_id: str) -> None:
        """Re-index one row for search. Caller holds the lock and transaction."""
        cur = self._conn.execute(
            "SELECT prompt, title, file_name FROM media WHERE media_id = ?",
            (media_id,),
        )
        row = cur.fetchone()
        self._conn.execute("DELETE FROM media_fts WHERE media_id = ?", (media_id,))
        if row is not None:
            self._conn.execute(
                "INSERT INTO media_fts (media_id, prompt, title, file_name) "
                "VALUES (?, ?, ?, ?)",
                (media_id, row["prompt"], row["title"], row["file_name"]),
            )

    def star(self, media_id: str, starred: bool = True) -> dict | None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media SET starred = ?, updated_at = ? WHERE media_id = ?",
                (1 if starred else 0, time.time(), media_id),
            )
        return self.get(media_id)

    def delete(self, media_id: str) -> bool:
        """Remove the row AND the bytes: file, poster sidecar, thumbnail.

        Deleting from the library is a real deletion. The chat bubble that
        referenced this media then renders the ``expired`` placeholder that
        already exists for retention-pruned files — one behaviour for "the
        media is gone", not two.
        """
        row = self.get(media_id)
        if row is None:
            return False

        from flowly.media.serving import resolve_media_id

        for name in (media_id, row.get("posterId") or ""):
            if not name:
                continue
            target, error, _status = resolve_media_id(name, self._media_dir)
            if target is None:
                logger.debug("[library] delete skipped {}: {}", name, error)
                continue
            try:
                target.unlink()
            except OSError as exc:
                logger.debug("[library] unlink failed for {}: {}", target, exc)

        self._drop_thumb(media_id)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM media WHERE media_id = ?", (media_id,))
            self._conn.execute("DELETE FROM media_fts WHERE media_id = ?", (media_id,))
        return True

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, media_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM media WHERE media_id = ?", (media_id,)
        )
        row = cur.fetchone()
        return _row_to_item(row) if row else None

    def list(
        self,
        *,
        kind: str | None = None,
        source: str | None = None,
        search: str | None = None,
        session_key: str | None = None,
        starred: bool | None = None,
        include_expired: bool = False,
        limit: int = 50,
        offset: int = 0,
        with_thumbs: bool = False,
    ) -> tuple[list[dict], int]:
        """A page of the library plus the total matching that filter.

        The total is what lets a client render "1–50 of 812" and decide whether
        another page exists without asking for one.
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        conditions: list[str] = []
        params: list[Any] = []
        join = ""

        term = (search or "").strip()
        if term:
            join = "JOIN media_fts ON media_fts.media_id = media.media_id"
            conditions.append("media_fts MATCH ?")
            params.append(_fts_query(term))
        if kind:
            conditions.append("media.kind = ?")
            params.append(kind)
        if source:
            conditions.append("media.source = ?")
            params.append(source)
        if session_key:
            conditions.append("media.session_key = ?")
            params.append(session_key)
        if starred is not None:
            conditions.append("media.starred = ?")
            params.append(1 if starred else 0)
        if not include_expired:
            conditions.append("media.status != ?")
            params.append(STATUS_EXPIRED)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        total_cur = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM media {join} {where}", params
        )
        total = int(total_cur.fetchone()["n"])

        cur = self._conn.execute(
            f"""SELECT media.* FROM media {join} {where}
                ORDER BY media.starred DESC, media.created_at DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        items = [_row_to_item(row) for row in cur.fetchall()]

        if with_thumbs:
            self._attach_thumbs(items)
        return items, total

    def stats(self) -> dict:
        """Per-kind counts and bytes, plus what the thumbnail cache costs.

        This is what turns a Library header into something honest — "1.2 GB ·
        340 items" — and gives media retention its first user-visible home.
        """
        cur = self._conn.execute(
            """SELECT kind, source, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes
               FROM media WHERE status != ? GROUP BY kind, source""",
            (STATUS_EXPIRED,),
        )
        by_kind: dict[str, dict[str, int]] = {}
        by_source: dict[str, int] = {SOURCE_GENERATED: 0, SOURCE_RECEIVED: 0}
        total_items = 0
        total_bytes = 0
        for row in cur.fetchall():
            bucket = by_kind.setdefault(row["kind"], {"count": 0, "bytes": 0})
            bucket["count"] += int(row["n"])
            bucket["bytes"] += int(row["bytes"])
            by_source[row["source"]] = by_source.get(row["source"], 0) + int(row["n"])
            total_items += int(row["n"])
            total_bytes += int(row["bytes"])

        expired = self._conn.execute(
            "SELECT COUNT(*) AS n FROM media WHERE status = ?", (STATUS_EXPIRED,)
        ).fetchone()["n"]

        return {
            "totalItems": total_items,
            "totalBytes": total_bytes,
            "expiredItems": int(expired),
            "byKind": by_kind,
            "bySource": by_source,
            "thumbnailBytes": _dir_size(thumbs_dir(self._media_dir)),
        }

    # ── Thumbnails ────────────────────────────────────────────────────────

    def _thumb_path(self, media_id: str) -> Path:
        # The media id is a validated basename by construction; ``.jpg`` on the
        # end keeps the cache self-describing and collision-free against ids
        # that differ only by extension (``clip.mp4`` vs ``clip.webm``).
        return thumbs_dir(self._media_dir) / f"{media_id}.jpg"

    def _drop_thumb(self, media_id: str) -> None:
        try:
            self._thumb_path(media_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("[library] thumb unlink failed for {}: {}", media_id, exc)

    def _attach_thumbs(self, items: list[dict]) -> None:
        """Fill in ``thumbnail`` for a page, generating what is missing.

        Bounded by :data:`MAX_THUMBS_PER_LIST` so a first load against a large
        cold library cannot become a hundred ffmpeg invocations in one RPC. A
        row left uncovered simply comes back without a thumbnail — clients
        already render a kind placeholder for that — and gets its turn on a
        later request.
        """
        budget = MAX_THUMBS_PER_LIST
        for item in items:
            media_id = item["mediaId"]
            path = self._thumb_path(media_id)
            if path.is_file():
                encoded = _read_thumb_b64(path)
                if encoded:
                    item["thumbnail"] = encoded
                    continue
                # Cached file is unreadable/empty — fall through and rebuild.
            if item.get("thumbState") == THUMB_NONE:
                continue
            if budget <= 0:
                continue
            budget -= 1
            if self._build_thumb(item):
                encoded = _read_thumb_b64(path)
                if encoded:
                    item["thumbnail"] = encoded

    def _build_thumb(self, item: dict) -> bool:
        """Render and cache one thumbnail. Records failure so it isn't retried."""
        media_id = item["mediaId"]
        source_path = self._source_for_thumb(item)
        dest = self._thumb_path(media_id)
        ok = False
        if source_path is not None:
            ok = _write_thumbnail(source_path, dest)
        self._set_thumb_state(media_id, THUMB_READY if ok else THUMB_NONE)
        item["thumbState"] = THUMB_READY if ok else THUMB_NONE
        return ok

    def _source_for_thumb(self, item: dict) -> Path | None:
        """The image a thumbnail is rendered from, extracting one if needed.

        An image is its own source. A video's is its poster — already on disk
        next to the clip for anything ``video_generate`` produced, extracted
        here on first request for anything else. Audio has no visual at all and
        never gets a thumbnail; the Audio tab draws a kind badge instead, which
        is both cheaper and more legible than a waveform at 160 pt.
        """
        from flowly.media.serving import resolve_media_id

        kind = item.get("kind")
        if kind == KIND_AUDIO:
            return None

        if kind == KIND_IMAGE:
            target, _error, _status = resolve_media_id(item["mediaId"], self._media_dir)
            return target

        poster_id = item.get("posterId") or ""
        if poster_id:
            target, _error, _status = resolve_media_id(poster_id, self._media_dir)
            if target is not None:
                return target

        clip, _error, _status = resolve_media_id(item["mediaId"], self._media_dir)
        if clip is None:
            return None
        from flowly.media.probe import extract_poster

        extracted = clip.with_suffix(".jpg")
        if extracted.is_file() or extract_poster(clip, extracted):
            self._set_poster(item["mediaId"], extracted.name)
            item["posterId"] = extracted.name
            return extracted
        return None

    def _set_thumb_state(self, media_id: str, state: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media SET thumb_state = ? WHERE media_id = ?",
                (state, media_id),
            )

    def _set_poster(self, media_id: str, poster_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media SET poster_id = ? WHERE media_id = ?",
                (poster_id, media_id),
            )

    # ── Reconcile ─────────────────────────────────────────────────────────

    def reconcile(self, *, probe_new: bool = True) -> dict:
        """Make the index agree with the disk. Never raises.

        Four passes, in this order:

        1. every servable file with no row gets one (``source=received`` — a
           file nobody claimed was generated is, by elimination, one that
           arrived);
        2. rows whose file is gone flip to ``expired`` rather than vanishing,
           so a chat bubble still linking to them can explain itself;
        3. rows that came back (a restored backup, a re-download) flip to
           ``ready`` again;
        4. thumbnails with no row are deleted.

        ``probe_new`` measures dimensions and duration for newly discovered
        files. It is on for the background pass and off in tests that only care
        about row bookkeeping.
        """
        summary = {"added": 0, "expired": 0, "restored": 0, "thumbs_pruned": 0}
        try:
            entries = scan_media_dir(self._media_dir)
        except Exception as exc:  # noqa: BLE001 - reconcile must never break startup
            logger.debug("[library] reconcile scan failed: {}", exc)
            return summary

        known = {
            row["media_id"]: row["status"]
            for row in self._conn.execute("SELECT media_id, status FROM media")
        }
        seen: set[str] = set()

        for entry in entries:
            media_id = entry.path.name
            seen.add(media_id)
            if media_id in known:
                if known[media_id] == STATUS_EXPIRED:
                    self._set_status(media_id, STATUS_READY)
                    summary["restored"] += 1
                continue
            width = height = duration = None
            if probe_new:
                from flowly.media.probe import probe as _probe

                measured = _probe(entry.path, entry.mime_type)
                width, height, duration = (
                    measured.width,
                    measured.height,
                    measured.duration_ms,
                )
            try:
                self._upsert(
                    media_id=media_id,
                    asset_id="",
                    kind=entry.kind,
                    source=SOURCE_RECEIVED,
                    mime_type=entry.mime_type,
                    file_name=media_id,
                    size=entry.size,
                    width=width,
                    height=height,
                    duration_ms=duration,
                    poster_id=entry.poster.name if entry.poster else "",
                    provider="",
                    model="",
                    prompt="",
                    session_key="",
                    channel="",
                    message_ts="",
                    created_at=entry.mtime,
                )
                summary["added"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[library] reconcile insert failed for {}: {}", media_id, exc)

        for media_id, status in known.items():
            if media_id in seen or status == STATUS_EXPIRED:
                continue
            self._set_status(media_id, STATUS_EXPIRED)
            self._drop_thumb(media_id)
            summary["expired"] += 1

        summary["thumbs_pruned"] = self._prune_thumbs(set(known) | seen)
        return summary

    def expire(self, media_ids: list[str]) -> int:
        """Flip named rows to ``expired`` and drop their thumbnails.

        The row survives on purpose: a chat bubble may still reference this
        media, and "no longer available" is a better answer than a card that
        silently disappeared from a history the user remembers.
        """
        expired = 0
        for media_id in media_ids:
            row = self.get(media_id)
            if row is None or row.get("status") == STATUS_EXPIRED:
                continue
            self._set_status(media_id, STATUS_EXPIRED)
            self._drop_thumb(media_id)
            expired += 1
        return expired

    def _set_status(self, media_id: str, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media SET status = ?, updated_at = ? WHERE media_id = ?",
                (status, time.time(), media_id),
            )

    def _prune_thumbs(self, live_ids: set[str]) -> int:
        directory = thumbs_dir(self._media_dir)
        if not directory.is_dir():
            return 0
        pruned = 0
        try:
            cached = list(directory.iterdir())
        except OSError:
            return 0
        for path in cached:
            if not path.is_file():
                continue
            # ``<media_id>.jpg`` — strip exactly the suffix we appended.
            owner = path.name[: -len(".jpg")] if path.name.endswith(".jpg") else path.name
            if owner in live_ids:
                continue
            try:
                path.unlink()
                pruned += 1
            except OSError:
                continue
        return pruned

    # ── Backfill ──────────────────────────────────────────────────────────

    def backfill_from_sessions(self, sessions_dir: Path, *, force: bool = False) -> dict:
        """Recover provenance for files that predate the index. Runs once.

        Before this module existed, everything a generator measured — provider,
        model, the originating chat — was written only into one message's
        ``media_assets`` metadata inside a session transcript. Those files are
        still on disk and a plain scan will find them, but as anonymous
        ``received`` rows with no prompt to search and no chat to jump back to.

        Walking the transcripts recovers all of it, so an existing install
        opens the Library on day one to a real history rather than a wall of
        untitled tiles. Guarded by a ``meta`` flag because it is seconds of I/O
        on a long-lived install and pointless twice.
        """
        summary = {"skipped": True, "sessions": 0, "enriched": 0}
        if not force and self._meta_get(_BACKFILL_KEY):
            return summary
        summary["skipped"] = False

        if not sessions_dir.is_dir():
            self._meta_set(_BACKFILL_KEY, str(int(time.time())))
            return summary

        from flowly.media.assets import assets_from_meta
        from flowly.session.manager import FULL_TRANSCRIPT_SUFFIX, iter_session_files

        try:
            canonical = list(iter_session_files(sessions_dir))
        except OSError as exc:
            logger.debug("[library] backfill listing failed: {}", exc)
            return summary

        for session_file in canonical:
            summary["sessions"] += 1
            session_key = _session_key_for(session_file)
            # Prefer the append-only DISPLAY transcript. The canonical file is
            # the LLM working context, which compaction rewrites as
            # ``[summary] + recent`` — so on any long conversation the early
            # turns, and the media they carried, are simply not in it.
            transcript = session_file.with_name(
                session_file.stem + FULL_TRANSCRIPT_SUFFIX
            )
            if not transcript.is_file():
                transcript = session_file
            try:
                lines = transcript.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("[library] backfill read failed for {}: {}", transcript, exc)
                continue
            for line in lines:
                if '"media_assets"' not in line:
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                assets = assets_from_meta(message.get("media_assets"))
                if not assets:
                    continue
                # An assistant turn carries what the agent PRODUCED; media on a
                # user turn is what the person attached.
                source = (
                    SOURCE_GENERATED
                    if message.get("role") == "assistant"
                    else SOURCE_RECEIVED
                )
                summary["enriched"] += self.record(
                    assets,
                    source=source,
                    session_key=session_key,
                    message_ts=_clean_str(message.get("timestamp"), 64),
                )

        self._meta_set(_BACKFILL_KEY, str(int(time.time())))
        return summary


# ── Row → wire ────────────────────────────────────────────────────────────────


def _row_to_item(row: sqlite3.Row) -> dict:
    """One row as the Attachment V2 shape a client already renders, plus
    provenance.

    Keeping the delivery half byte-identical to what a chat bubble receives is
    the whole reason the Library needed no new transport: a client hands this
    object to the same attachment component, and ``mediaId`` resolves through
    the same three doors (hosted, gateway ticket, relay bridge).
    """
    item: dict[str, Any] = {
        "version": 2,
        "id": row["media_id"],
        "mediaId": row["media_id"],
        "kind": row["kind"],
        "fileName": row["file_name"],
        "mimeType": row["mime_type"],
        "status": row["status"],
        "size": int(row["size"] or 0),
        # provenance — library-only, never on the chat wire
        "source": row["source"],
        "starred": bool(row["starred"]),
        "createdAt": float(row["created_at"] or 0.0),
        "thumbState": row["thumb_state"],
    }
    for column, key in (
        ("width", "width"),
        ("height", "height"),
        ("duration_ms", "durationMs"),
    ):
        value = _int_or_none(row[column])
        if value is not None:
            item[key] = value
    for column, key in (
        ("poster_id", "posterId"),
        ("provider", "provider"),
        ("model", "model"),
        ("prompt", "prompt"),
        ("title", "title"),
        ("session_key", "sessionKey"),
        ("channel", "channel"),
        ("message_ts", "messageTs"),
    ):
        value = row[column]
        if value:
            item[key] = value
    return item


def _fts_query(term: str) -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    Every token is quoted, so punctuation a user typed cannot be read as FTS
    syntax and turn a search into a syntax error. Tokens are ANDed: typing more
    words narrows, which is what a search box is expected to do.
    """
    tokens = [t.replace('"', "") for t in term.split() if t.strip()]
    return " AND ".join(f'"{t}"*' for t in tokens[:12]) or '""'


def _session_key_for(session_file: Path) -> str:
    """Recover a session key from its transcript filename.

    Session keys are ``channel:chat_id``; ``SessionManager._get_session_path``
    writes them through ``safe_filename(key.replace(":", "_"))``, and
    ``safe_filename`` only substitutes ``<>:"/\\|?*``. Channel names never
    contain an underscore, so restoring the colon is a partition on the FIRST
    one — a chat id that itself contains underscores survives intact.

    A filename that does not fit the pattern yields ``''``. The row is still
    indexed; it just cannot offer "Open in chat".
    """
    stem = session_file.stem
    channel, separator, rest = stem.partition("_")
    if separator and channel and rest:
        return f"{channel}:{rest}"
    return ""


def _read_thumb_b64(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _write_thumbnail(source: Path, dest: Path) -> bool:
    """Render a small JPEG preview of *source*. False when it can't be done."""
    try:
        from PIL import Image

        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))
            img.save(dest, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001 - a missing preview is not an error
        logger.debug("[library] thumbnail failed for {}: {}", source, exc)
        return False


def _dir_size(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    total = 0
    try:
        for path in directory.iterdir():
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total
