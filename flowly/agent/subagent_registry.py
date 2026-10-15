"""Subagent run registry — disk-persistent, crash-safe."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock
from loguru import logger

from flowly.agent.subagent_threads import (
    MAX_PENDING_MAIL,
    THREAD_IDLE_CLOSE_SECONDS,
    THREAD_RETENTION_SECONDS,
    SubagentThreadRecord,
    SubagentThreadStore,
    decode_thread,
    is_thread_id,
    last_active,
)


def _default_registry_path() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "subagents" / "runs.json"

_REGISTRY_PATH = None  # resolved lazily
# A finished run, its result and its transcript are kept this long: long
# enough to reopen a conversation days later and still open its subagents.
_PRUNE_AFTER_SECONDS = 30 * 86_400
# System work (self-review, Board) and runs started with cleanup="delete" are
# bookkeeping nobody reopens; they hold conversation and memory text, so they
# go after a day.
_SYSTEM_PRUNE_AFTER_SECONDS = 86_400
_SETTLED_DELIVERY = frozenset({"not_required", "delivered", "undelivered"})
# A result's delivery: pending → queued (its notice was published) → taken
# (agent_wait handed it to a turn not saved yet) → delivered (the
# conversation saved the turn that handled it); or undelivered, or
# not_required (nobody waits for it).


def append_jsonl(path: Path, line: bytes) -> None:
    """Append one JSON line to an append-only file, mode 0600.

    A crash can leave the last line without its newline. Appending straight
    after it would glue the new record onto the torn one and lose both, so a
    record that follows a torn line starts on a line of its own; readers
    skip the torn fragment as an unreadable line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                line = b"\n" + line
        os.write(fd, line)
    finally:
        os.close(fd)


def _retention_seconds(record: "SubagentRunRecord") -> int:
    if record.origin == "system" or record.cleanup == "delete":
        return _SYSTEM_PRUNE_AFTER_SECONDS
    return _PRUNE_AFTER_SECONDS


@dataclass
class SubagentRunRecord:
    run_id: str
    child_session_key: str   # "subagent:{run_id}"
    parent_session_key: str  # "telegram:123456"
    parent_channel: str
    parent_chat_id: str
    task: str
    label: str               # internal/dedup key (e.g. "builtin:researcher")
    model: str | None
    cleanup: str             # "keep" | "delete"
    created_at: float
    # User-facing name, always task-derived (never a code/UUID). Falls back to
    # ``label`` only for older persisted records that predate this field.
    display_name: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    outcome: str | None = None   # "ok" | "error" | "timeout"
    error: str | None = None
    announced: bool = False
    # P1.2 — structured audit trail of every tool call the subagent made.
    # Each entry: {tool, args_bytes, result_bytes, status, duration_ms}.
    # Replaces the prior write-only list[str] that only logged names.
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "subagent"
    agent_id: str | None = None
    updated_at: float | None = None
    revision: int = 0
    activity: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    result_preview: str = ""
    result_chars: int = 0
    result_available: bool = False
    artifact_ids: list[str] = field(default_factory=list)
    delivery_state: str = "pending"
    # Who started it: "owner" (asked for it), "agent" (split its own work) or
    # "system" (self-review, Board, a scheduled run). Clients show the first
    # two in the conversation that started them.
    origin: str = "agent"
    # The shape and hue every client draws for this run: {"glyph", "hue"}.
    mark: dict[str, Any] = field(default_factory=dict)
    # Runs started in the same turn share it and report back together.
    batch_id: str = ""
    # Tokens the run has used so far: {"input", "output", "cacheRead", "total"}.
    usage: dict[str, int] = field(default_factory=dict)
    # The process working on it (subagent_owner.process_owner): only that
    # process works on the run; another may take it over only once the
    # owner is provably gone. Empty for runs recorded before owners existed.
    owner: dict[str, Any] = field(default_factory=dict)
    # Times a startup announced this finished result again because the
    # conversation never saved a turn that handled it.
    reannounced: int = 0
    # Continued after a restart (subagent_checkpoint): set at spawn for
    # owner/agent runs of a gateway that report back; cleared if its
    # checkpoint cannot be kept.
    resumable: bool = False
    # Restarts it was continued across with no step finished since (a
    # crash loop is bounded by it), when a clean shutdown suspended it, the
    # downtime left out of its duration, and when it last continued.
    resume_count: int = 0
    suspended_at: float | None = None
    suspended_seconds: float = 0.0
    resumed_at: float | None = None
    # The thread this run is a turn of (subagent_threads): its id is the
    # thread's first run_id. Empty for system runs, delegated tasks and runs
    # recorded before threads existed. ``turn`` counts from 1 (the spawn);
    # ``trigger`` is "spawn" or "followup".
    thread_id: str = ""
    turn: int = 1
    trigger: str = "spawn"
    # A follow-up turn's messages from the main agent that were waiting for
    # it (the thread's pending mail, taken when the turn was recorded): its
    # first message carries them before the task.
    notes: list[str] = field(default_factory=list)
    # Who stopped a cancelled run: "owner" (from a client) or "agent" (its
    # main agent, or closing its subagent). The main agent is told of an
    # owner's stop; its own it already knows.
    stopped_by: str = ""



_USAGE_KEYS = ("input", "output", "cacheRead", "total")


def add_usage(totals: dict[str, int], usage: dict[str, Any]) -> dict[str, int]:
    """A run's token totals after one more model call.

    Providers report ``prompt_tokens`` / ``completion_tokens`` (and, for
    some, cache reads apart from the prompt); the total is what they say, or
    the sum when they do not say.
    """
    def count(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    prompt, completion, cache_read = count("prompt_tokens"), count("completion_tokens"), count("cache_read_tokens")
    total = count("total_tokens") or prompt + completion
    merged = {key: int(totals.get(key, 0)) for key in _USAGE_KEYS}
    merged["input"] += prompt
    merged["output"] += completion
    merged["cacheRead"] += cache_read
    merged["total"] += total
    return merged


class ThreadCapReached(RuntimeError):
    """Every open thread of the conversation has a task running."""

    def __init__(self, open_threads: int) -> None:
        super().__init__(f"{open_threads} open subagent threads, none idle")
        self.open_threads = open_threads


class SubagentRegistry:
    """Atomic, locked snapshots; result bodies live outside the list index.

    Persistence errors propagate: a successful API reply must mean the change
    survived on disk. A broken index is never silently overwritten with [] .
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_registry_path()
        self._transcript_lock = threading.Lock()
        self._transcript_seq: dict[str, int] = {}
        # run_id → (seq, byte offset just past that step): a client polling
        # with the cursor it last got resumes there instead of re-reading the
        # whole transcript on every poll.
        self._transcript_cursor: dict[str, tuple[int, int]] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self._path) + ".lock", timeout=5)
        self._runs: dict[str, SubagentRunRecord] = {}
        # Subagent threads (threads.json), written under the same lock as
        # the runs. A threads file that cannot be read is never overwritten
        # and nothing is pruned on its account; runs keep working without
        # threads until it is repaired.
        self._threads: dict[str, SubagentThreadRecord] = {}
        self._threads_unavailable = False
        self._threads_disk: bytes | None = None
        # The thread ids the thread files were last pruned against.
        self._pruned_for: frozenset[str] | None = None
        self.thread_store = SubagentThreadStore(self._path.parent)
        try:
            self._load_from_disk()
        except (OSError, ValueError):
            # Keep the host usable. Reads/writes still raise until the index is
            # repaired; an unavailable history must never become an empty one.
            logger.exception("Subagent history is unavailable")

    @staticmethod
    def _decode(item: Any) -> SubagentRunRecord:
        if not isinstance(item, dict):
            raise ValueError("invalid run record")
        known = {k: item[k] for k in SubagentRunRecord.__dataclass_fields__ if k in item}
        record = SubagentRunRecord(**known)
        for name in ("run_id", "task", "label", "display_name", "parent_session_key",
                     "parent_channel", "parent_chat_id", "child_session_key", "kind"):
            if not isinstance(getattr(record, name), str):
                raise ValueError("invalid run text")
        if not record.run_id or record.created_at is None:
            raise ValueError("missing run identity")
        for name in ("created_at", "started_at", "ended_at", "updated_at", "suspended_at", "resumed_at"):
            value = getattr(record, name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("invalid run timestamp")
        for name in ("model", "outcome", "error", "error_code", "agent_id"):
            if getattr(record, name) is not None and not isinstance(getattr(record, name), str):
                raise ValueError("invalid optional run text")
        if not isinstance(record.result_preview, str) or type(record.result_chars) is not int or record.result_chars < 0:
            raise ValueError("invalid run result")
        if type(record.result_available) is not bool or type(record.announced) is not bool:
            raise ValueError("invalid run flags")
        if type(record.revision) is not int or record.revision < 0:
            raise ValueError("invalid revision")
        if not isinstance(record.activity, dict) or not isinstance(record.tool_trace, list):
            raise ValueError("invalid run activity")
        if any(not isinstance(step, dict) or not isinstance(step.get("tool", ""), str)
               for step in record.tool_trace):
            raise ValueError("invalid tool history")
        if not isinstance(record.artifact_ids, list) or any(not isinstance(x, str) for x in record.artifact_ids):
            raise ValueError("invalid artifact references")
        if record.origin not in ("owner", "agent", "system"):
            raise ValueError("invalid run origin")
        if not isinstance(record.mark, dict):
            raise ValueError("invalid run mark")
        if not isinstance(record.batch_id, str):
            raise ValueError("invalid run batch")
        if not isinstance(record.usage, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0 for k, v in record.usage.items()
        ):
            raise ValueError("invalid run usage")
        if not isinstance(record.delivery_state, str):
            raise ValueError("invalid delivery state")
        if not isinstance(record.owner, dict):
            raise ValueError("invalid run owner")
        if type(record.reannounced) is not int or record.reannounced < 0:
            raise ValueError("invalid run counters")
        if type(record.resumable) is not bool:
            raise ValueError("invalid run flags")
        if type(record.resume_count) is not int or record.resume_count < 0:
            raise ValueError("invalid run counters")
        if (type(record.suspended_seconds) not in (int, float) or not math.isfinite(record.suspended_seconds)
                or record.suspended_seconds < 0):
            raise ValueError("invalid run downtime")
        if not isinstance(record.thread_id, str) or (record.thread_id and not is_thread_id(record.thread_id)):
            raise ValueError("invalid run thread")
        if type(record.turn) is not int or record.turn < 1 or record.trigger not in ("spawn", "followup"):
            raise ValueError("invalid run turn")
        if not isinstance(record.notes, list) or any(not isinstance(n, str) for n in record.notes):
            raise ValueError("invalid run notes")
        return record

    def _read_locked(self) -> None:
        records = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("Invalid subagent registry")
            for item in raw:
                try:
                    record = self._decode(item)
                except (TypeError, ValueError):
                    logger.warning("Skipping invalid subagent history record")
                    continue
                records[record.run_id] = record
        now = time.time()
        records = {
            key: rec for key, rec in records.items()
            if rec.ended_at is None or rec.ended_at >= now - _retention_seconds(rec)
            or not (rec.announced or rec.delivery_state in _SETTLED_DELIVERY)
        }
        # Preserve references held by the live manager/board.
        for key, rec in records.items():
            if key in self._runs:
                self._runs[key].__dict__.update(rec.__dict__)
                records[key] = self._runs[key]
        self._runs = records
        self._read_threads_locked(now)

    @property
    def _threads_path(self) -> Path:
        return self._path.with_name("threads.json")

    def _read_threads_locked(self, now: float) -> None:
        """Load threads.json and bring it up to date with the runs.

        Like the runs' pruning, what this decides is kept in memory and
        written with the next change: a thread whose run is gone or ended
        no longer has a task running; an open thread idle for
        THREAD_IDLE_CLOSE_SECONDS is closed (``idle``); a thread closed
        THREAD_RETENTION_SECONDS ago whose results were all delivered is
        dropped (its files go with the next write).
        """
        threads: dict[str, SubagentThreadRecord] = {}
        raw_bytes: bytes | None = None
        path = self._threads_path
        try:
            if path.exists():
                raw_bytes = path.read_bytes()
                raw = json.loads(raw_bytes)
                if not isinstance(raw, list):
                    raise ValueError("Invalid subagent threads")
                for item in raw:
                    try:
                        thread = decode_thread(item)
                    except ValueError:
                        logger.warning("Skipping invalid subagent thread record")
                        continue
                    threads[thread.thread_id] = thread
        except (OSError, ValueError):
            if not self._threads_unavailable:
                logger.exception("Subagent threads are unavailable; runs continue without them")
            self._threads_unavailable = True
            self._threads = {}
            self._threads_disk = None
            return
        self._threads_unavailable = False
        self._threads_disk = raw_bytes
        for thread in threads.values():
            active = self._runs.get(thread.active_run_id) if thread.active_run_id else None
            if thread.active_run_id and (active is None or active.ended_at is not None):
                if active is not None:
                    self._close_out_run(thread, active)
                else:
                    thread.active_run_id = None
            if (thread.status == "open" and thread.active_run_id is None
                    and now - last_active(thread) >= THREAD_IDLE_CLOSE_SECONDS):
                closed_at = last_active(thread) + THREAD_IDLE_CLOSE_SECONDS
                thread.status, thread.closed_at, thread.closed_reason = "closed", closed_at, "idle"
                thread.pending_mail = []
                thread.updated_at = closed_at
                thread.revision += 1
        kept: dict[str, SubagentThreadRecord] = {}
        for thread_id, thread in threads.items():
            expired = (
                thread.status == "closed" and thread.active_run_id is None
                and (thread.closed_at or thread.created_at) + THREAD_RETENTION_SECONDS <= now
            )
            # A result of the thread still waiting to be delivered keeps it.
            waiting = any(
                (run := self._runs.get(run_id)) is not None
                and (run.ended_at is None or not (run.announced or run.delivery_state in _SETTLED_DELIVERY))
                for run_id in thread.runs
            )
            if not expired or waiting:
                kept[thread_id] = thread
        self._threads = kept

    @staticmethod
    def _close_out_run(thread: SubagentThreadRecord, run: SubagentRunRecord) -> None:
        """A run of the thread ended: nothing runs on it now; its tokens count."""
        if thread.active_run_id != run.run_id:
            return
        thread.active_run_id = None
        thread.last_run_ended_at = run.ended_at
        thread.usage = {k: int(thread.usage.get(k, 0)) + int(run.usage.get(k, 0))
                        for k in ("input", "output", "cacheRead", "total")}
        thread.updated_at = run.ended_at
        thread.revision += 1

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp." + secrets.token_hex(8))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            if os.name == "posix":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    def _write_locked(self) -> None:
        if not self._threads_unavailable:
            # Threads first: a crash between the two writes leaves a thread
            # whose run is missing (it reads as idle), never a run pointing
            # at a thread that was never written.
            threads = json.dumps([asdict(t) for t in self._threads.values()],
                                 ensure_ascii=False).encode("utf-8")
            if threads != self._threads_disk and (self._threads or self._threads_disk is not None):
                self._atomic_write(self._threads_path, threads)
                self._threads_disk = threads
        data = json.dumps([asdict(r) for r in self._runs.values()], ensure_ascii=False)
        self._atomic_write(self._path, data.encode("utf-8"))
        if not self._threads_unavailable:
            keep = frozenset(self._threads)
            if keep != self._pruned_for:
                try:
                    self.thread_store.prune(set(keep))
                    self._pruned_for = keep
                except OSError:
                    logger.warning("Could not prune expired subagent conversations")
        # Only our own hashed files are eligible; never follow arbitrary paths.
        keep = {self._result_path(r.run_id).name for r in self._runs.values()}
        if self._result_dir.exists():
            for path in self._result_dir.glob("*.txt"):
                if re.fullmatch(r"[0-9a-f]{64}\.txt", path.name) and path.name not in keep:
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Could not prune an expired subagent result")
        keep_transcripts = {self._transcript_path(r.run_id).name for r in self._runs.values()}
        if self._transcript_dir.exists():
            for path in self._transcript_dir.glob("*.jsonl"):
                if re.fullmatch(r"[0-9a-f]{64}\.jsonl", path.name) and path.name not in keep_transcripts:
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Could not prune an expired subagent transcript")
        # A checkpoint lives only while its run is running: one left behind by
        # a crash after the run ended (or whose run is gone) is removed here.
        keep_checkpoints = {
            self.checkpoint_path(r.run_id).name for r in self._runs.values() if r.ended_at is None
        }
        if self._checkpoint_dir.exists():
            for path in self._checkpoint_dir.glob("*.jsonl"):
                if re.fullmatch(r"[0-9a-f]{64}\.jsonl", path.name) and path.name not in keep_checkpoints:
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Could not prune a finished subagent checkpoint")

    @property
    def _result_dir(self) -> Path:
        return self._path.with_name(self._path.stem + "-results")

    def _result_path(self, run_id: str) -> Path:
        return self._result_dir / (hashlib.sha256(run_id.encode()).hexdigest() + ".txt")

    @property
    def _transcript_dir(self) -> Path:
        return self._path.with_name(self._path.stem + "-transcripts")

    def _transcript_path(self, run_id: str) -> Path:
        return self._transcript_dir / (hashlib.sha256(run_id.encode()).hexdigest() + ".jsonl")

    @property
    def _checkpoint_dir(self) -> Path:
        return self._path.with_name(self._path.stem + "-checkpoints")

    def checkpoint_path(self, run_id: str) -> Path:
        """Where a running run's checkpoint lives (subagent_checkpoint)."""
        return self._checkpoint_dir / (hashlib.sha256(run_id.encode()).hexdigest() + ".jsonl")

    def append_transcript(self, run_id: str, item: dict[str, Any]) -> int:
        """Append one step of a run's transcript; returns its sequence number.

        The transcript is what a client shows when the owner opens a
        subagent: its brief, what it said, the tools it called, messages
        exchanged with the main agent and its result. Only the run's owner
        writes it, so a process-local lock is enough. After a restart the
        sequence continues from the last readable step, so it stays
        gapless, and a step torn by a crash is left behind on its own line.
        """
        with self._transcript_lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            seq = self._transcript_seq.get(run_id)
            path = self._transcript_path(run_id)
            if seq is None:
                seq = self._last_transcript_seq(path)
            seq += 1
            entry = {"seq": seq, "ts": time.time(), **item}
            append_jsonl(path, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            self._transcript_seq[run_id] = seq
            return seq

    @staticmethod
    def _last_transcript_seq(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            # Written before transcripts were private.
            os.chmod(path, 0o600)
        except OSError:
            pass
        last = 0
        with path.open("rb") as stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict) and type(entry.get("seq")) is int:
                    last = max(last, entry["seq"])
        return last

    def read_transcript(self, run_id: str, after: int = 0, limit: int = 200) -> dict[str, Any]:
        """Transcript steps with ``seq > after``, oldest first, at most ``limit``."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid transcript range")
        record = next((r for r in self.all() if r.run_id == run_id), None)
        if record is None:
            raise FileNotFoundError("No such run")
        items: list[dict[str, Any]] = []
        path = self._transcript_path(run_id)
        more = False
        if path.exists():
            cached = self._transcript_cursor.get(run_id)
            start = cached[1] if cached and cached[0] == after else 0
            with path.open("rb") as stream:
                stream.seek(start)
                position = start
                last_seq, last_end = after, start
                while True:
                    raw = stream.readline()
                    if not raw:
                        break
                    position += len(raw)
                    if not raw.endswith(b"\n"):
                        break  # a step still being written, or torn by a crash
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict) or int(entry.get("seq", 0)) <= after:
                        continue
                    if len(items) >= limit:
                        more = True
                        break
                    items.append(entry)
                    last_seq, last_end = int(entry["seq"]), position
            if items:
                self._transcript_cursor[run_id] = (last_seq, last_end)
        last = items[-1]["seq"] if items else after
        return {"runId": run_id, "items": items, "nextSeq": last,
                "more": more, "done": record.ended_at is not None}

    def register(self, record: SubagentRunRecord, *, thread: SubagentThreadRecord | None = None,
                 open_cap: int | None = None) -> list[SubagentThreadRecord]:
        """Record a new run — and, with ``thread``, the new thread it starts.

        ``open_cap`` bounds the open threads of the thread's conversation: at
        the cap, the ones idle longest are closed (``cap``) to make room, and
        returned. :class:`ThreadCapReached` when every open thread is busy.
        A run whose ``thread_id`` names an existing open thread becomes its
        next turn: the messages waiting for it (``pending_mail``) move into
        the run's ``notes`` in the same write. Threads unavailable
        (threads.json unreadable): the run is recorded without one.
        """
        with self._lock:
            self._read_locked()
            if record.run_id in self._runs:
                raise ValueError("Duplicate subagent run ID")
            if self._threads_unavailable:
                record.thread_id, thread = "", None
            self._decode(asdict(record))
            evicted: list[SubagentThreadRecord] = []
            now = time.time()
            if thread is not None:
                if thread.thread_id in self._threads or thread.thread_id != record.thread_id:
                    raise ValueError("Invalid subagent thread")
                thread.runs = [record.run_id]
                thread.active_run_id = record.run_id
                thread.revision, thread.updated_at = 1, record.created_at
                decode_thread(asdict(thread))
                if open_cap is not None:
                    evicted = self._make_room_locked(thread.parent_session_key, open_cap, now)
                self._threads[thread.thread_id] = thread
            elif record.thread_id:
                existing = self._threads.get(record.thread_id)
                if existing is None or existing.status != "open" or existing.active_run_id is not None:
                    raise ValueError("This subagent's thread cannot take a new run")
                existing.runs.append(record.run_id)
                existing.active_run_id = record.run_id
                # Its number is decided here, under the lock: two processes
                # sharing a home never give one thread the same turn twice.
                record.turn = len(existing.runs)
                existing.model = record.model or existing.model
                # Messages that waited become its notes; kept tasks wait for
                # their own turn (SubagentManager._start_pending_tasks).
                record.notes = [*record.notes, *(str(m["text"]) for m in existing.pending_mail
                                                 if m.get("kind") != "task")]
                existing.pending_mail = [m for m in existing.pending_mail if m.get("kind") == "task"]
                existing.revision += 1
                existing.updated_at = record.created_at
            record.updated_at = record.created_at
            record.revision = 1
            self._runs[record.run_id] = record
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise
            return evicted

    def _make_room_locked(self, parent_session_key: str, cap: int, now: float) -> list[SubagentThreadRecord]:
        open_threads = [t for t in self._threads.values()
                        if t.parent_session_key == parent_session_key and t.status == "open"]
        if len(open_threads) < cap:
            return []
        idle = sorted((t for t in open_threads if t.active_run_id is None), key=last_active)
        excess = len(open_threads) - cap + 1
        if len(idle) < excess:
            raise ThreadCapReached(len(open_threads))
        evicted = idle[:excess]
        for thread in evicted:
            self._close_locked(thread, "cap", now)
        return evicted

    def take_pending_tasks(self, thread_id: str) -> list[dict[str, Any]]:
        """Take the tasks kept for an open, idle thread (oldest first); [] when none or not idle."""
        with self._lock:
            self._read_locked()
            thread = self._threads.get(thread_id)
            if self._threads_unavailable or thread is None or thread.status != "open" or thread.active_run_id:
                return []
            tasks = [m for m in thread.pending_mail if m.get("kind") == "task"]
            if not tasks:
                return []
            thread.pending_mail = [m for m in thread.pending_mail if m.get("kind") != "task"]
            thread.revision += 1
            thread.updated_at = time.time()
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise
            return tasks

    def requeue_tasks(self, thread_id: str, tasks: list[dict[str, Any]]) -> None:
        """Keep again tasks that could not start (ahead of anything kept since); dropped if it closed."""
        with self._lock:
            self._read_locked()
            thread = self._threads.get(thread_id)
            if self._threads_unavailable or thread is None or thread.status != "open" or not tasks:
                return
            thread.pending_mail = [*tasks, *thread.pending_mail][-MAX_PENDING_MAIL:]
            thread.revision += 1
            thread.updated_at = time.time()
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise

    def _close_locked(self, thread: SubagentThreadRecord, reason: str, now: float) -> None:
        """Close ``thread``; its runs' views change (``threadStatus``), so their revisions move."""
        thread.status, thread.closed_at, thread.closed_reason = "closed", now, reason
        thread.pending_mail = []
        thread.revision += 1
        thread.updated_at = now
        for run_id in thread.runs:
            run = self._runs.get(run_id)
            if run is not None:
                run.revision += 1
                run.updated_at = now

    def close_thread(self, thread_id: str, reason: str) -> SubagentThreadRecord | None:
        """Close an open thread (``agent_close``: ``agent``); None when it is not open.

        A task still running on it is the caller's to stop. Its history stays
        readable; it takes no new task or message.
        """
        with self._lock:
            self._read_locked()
            thread = self._threads.get(thread_id)
            if self._threads_unavailable or thread is None or thread.status != "open":
                return None
            self._close_locked(thread, reason, time.time())
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise
            return thread

    def queue_mail(self, thread_id: str, text: str, *, kind: str = "message", batch: str = "",
                   origin: str = "agent") -> tuple[SubagentThreadRecord | None, str]:
        """Keep a message (or, ``kind="task"``, a task) from the main agent for an open thread's next turn.

        ``(thread, "")`` when kept; ``(None, why)`` otherwise: ``closed``,
        ``running`` (a task runs: its inbox takes it), ``full``, ``unknown``.
        """
        with self._lock:
            self._read_locked()
            thread = self._threads.get(thread_id)
            if self._threads_unavailable or thread is None:
                return None, "unknown"
            if thread.status != "open":
                return None, "closed"
            if thread.active_run_id is not None:
                return None, "running"
            if len(thread.pending_mail) >= MAX_PENDING_MAIL:
                return None, "full"
            now = time.time()
            entry: dict[str, Any] = {"id": secrets.token_hex(8), "text": text, "at": now}
            if kind == "task":
                entry.update(kind="task", batch=batch, origin=origin)
            thread.pending_mail.append(entry)
            thread.revision += 1
            thread.updated_at = now
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise
            return thread, ""

    def update(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None:
                return
            if record.ended_at is not None:
                changes = {k: v for k, v in changes.items()
                           if k in ("announced", "delivery_state", "reannounced")}
            if not changes:
                return
            for key, value in changes.items():
                if key in record.__dataclass_fields__ and key not in ("run_id", "revision", "created_at"):
                    setattr(record, key, value)
            if record.ended_at is not None:
                for entry in record.tool_trace:
                    if entry.get("status") == "running":
                        entry.update(status=record.outcome or "interrupted", ended_at=record.ended_at)
                record.activity = {"phase": "finished"}
                thread = self._threads.get(record.thread_id) if record.thread_id else None
                if thread is not None:
                    self._close_out_run(thread, record)
            record.updated_at = time.time()
            record.revision += 1
            try:
                self._decode(asdict(record))
                self._write_locked()
            except Exception:
                self._read_locked()
                raise

    def finish(self, run_id: str, outcome: str, *, result: str = "",
               error: str | None = None, error_code: str | None = None,
               report: bool = False, stopped_by: str = "") -> None:
        """Record a run's end. A cancelled or interrupted run needs no
        delivery — unless ``report``: a run a restart could not continue is
        interrupted, and one its owner stopped is cancelled, and its
        conversation must hear so once."""
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None or record.ended_at is not None:
                return
            if result:
                self._atomic_write(self._result_path(run_id), result.encode("utf-8"))
            self.update(run_id, ended_at=time.time(), outcome=outcome, error=error,
                        error_code=error_code, result_preview=result[:1200],
                        result_chars=len(result), result_available=bool(result),
                        **({"stopped_by": stopped_by} if stopped_by else {}),
                        delivery_state=(
                            "not_required" if outcome in ("cancelled", "interrupted") and not report
                            else record.delivery_state
                        ))

    def claim(self, run_id: str, owner: dict[str, Any], expected_owner: dict[str, Any],
              **changes: Any) -> SubagentRunRecord | None:
        """Take a run over from ``expected_owner``; None when someone else did.

        Compare-and-set under the index lock: two processes recovering the
        same home at once see the same dead owner, and only the first one
        to claim gets the run. ``changes`` are applied in the same write.
        """
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None or record.owner != expected_owner:
                return None
            if record.ended_at is not None:
                changes = {k: v for k, v in changes.items()
                           if k in ("announced", "delivery_state", "reannounced")}
            for key, value in {"owner": owner, **changes}.items():
                if key in record.__dataclass_fields__ and key not in ("run_id", "revision", "created_at"):
                    setattr(record, key, value)
            record.updated_at = time.time()
            record.revision += 1
            try:
                self._decode(asdict(record))
                self._write_locked()
            except Exception:
                self._read_locked()
                raise
            return record

    def read_result(self, run_id: str, offset: int = 0, limit: int = 32000) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200000:
            raise ValueError("Invalid result range")
        with self._lock:
            self._read_locked()
            record = self._runs.get(run_id)
            if record is None or not record.result_available:
                raise FileNotFoundError("No saved result for this run")
            # Bounded memory even for large responses; offsets count Unicode characters.
            with self._result_path(run_id).open(encoding="utf-8") as stream:
                remaining = offset
                while remaining:
                    chunk = stream.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                content = stream.read(limit)
            end = offset + len(content)
            return {"runId": run_id, "content": content, "offset": offset,
                    "totalChars": record.result_chars,
                    "nextOffset": end if end < record.result_chars else None}

    def get(self, run_id: str) -> SubagentRunRecord | None:
        return self._runs.get(run_id)

    def all(self) -> list[SubagentRunRecord]:
        self._load_from_disk()
        return list(self._runs.values())

    def latest_by_label(self, label: str) -> SubagentRunRecord | None:
        return max((r for r in self.all() if label and r.label == label),
                   key=lambda r: r.created_at, default=None)

    def pending(self) -> list[SubagentRunRecord]:
        return [r for r in self.all() if r.ended_at is None]

    def _load_from_disk(self) -> None:
        with self._lock:
            self._read_locked()

    # ── Threads ─────────────────────────────────────────────────────────

    def get_thread(self, thread_id: str) -> SubagentThreadRecord | None:
        return self._threads.get(thread_id)

    def threads(self) -> list[SubagentThreadRecord]:
        self._load_from_disk()
        return list(self._threads.values())

    def thread_of(self, run_id: str) -> SubagentThreadRecord | None:
        record = self._runs.get(run_id)
        if record is None or not record.thread_id:
            return None
        return self._threads.get(record.thread_id)

    def threads_for_parent(self, parent_session_key: str) -> list[SubagentThreadRecord]:
        """A conversation's threads, oldest first."""
        return sorted((t for t in self.threads() if t.parent_session_key == parent_session_key),
                      key=lambda t: t.created_at)

    def runs_of_thread(self, thread: SubagentThreadRecord) -> list[SubagentRunRecord]:
        """The thread's runs still in the registry, oldest first."""
        return [run for run_id in thread.runs if (run := self._runs.get(run_id)) is not None]

    def forget_parent(self, parent_session_key: str) -> list[str]:
        """The conversation was deleted: its threads close (``parent_deleted``).

        Returns every thread id of that conversation, open or closed, so the
        caller removes their conversations and raw outputs now; the records
        stay (closed) for their retention period.
        """
        with self._lock:
            self._read_locked()
            if self._threads_unavailable:
                return []
            now = time.time()
            owned = [t for t in self._threads.values() if t.parent_session_key == parent_session_key]
            for thread in owned:
                if thread.status == "open":
                    self._close_locked(thread, "parent_deleted", now)
            if owned:
                try:
                    self._write_locked()
                except Exception:
                    self._read_locked()
                    raise
            return [t.thread_id for t in owned]

    def maintain(self) -> None:
        """Persist what reading decided: idle closes, expired threads and their files."""
        with self._lock:
            self._read_locked()
            try:
                self._write_locked()
            except Exception:
                self._read_locked()
                raise
