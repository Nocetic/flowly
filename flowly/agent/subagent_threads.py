"""A subagent's thread: its own conversation, and its edge to the parent.

Every subagent the main agent (or the owner) starts is a thread. Its id is
the first run's ``run_id``, so its conversation keeps the key every client
already knows, ``subagent:<threadId>`` (``SubagentRunRecord.child_session_key``).

* **The conversation** is a real :class:`~flowly.session.manager.Session`,
  written with the same machinery as the owner's conversations
  (``extend_with_turn_messages``, the append-only ``.full.jsonl`` display
  log, ``history_page``), but stored apart, in ``<home>/subagents/sessions/``.
  Nothing that reads the owner's conversations — ``sessions.list``,
  ``session_search`` and its index, the MCP read plane, memory extraction,
  the dreamer — looks there. The directory is 0700 and its files 0600.
* **Its raw tool outputs** — what did not fit into what the model was given —
  are spilled into ``<home>/subagents/spill/<threadId>/`` and live exactly as
  long as the conversation.
* **The edge** is a :class:`SubagentThreadRecord` in ``threads.json``, next to
  ``runs.json`` and written under the same lock by
  :class:`~flowly.agent.subagent_registry.SubagentRegistry`. A thread is
  ``open`` or ``closed``. Runs (``SubagentRunRecord``) are its turns: delivery,
  batching and metrics stay per run.

Only the agent that started a thread writes to it, through its runs; every
client may read it and none may write (``flowly.session.keys``).

**Later turns.** A follow-up turn starts from the thread's conversation as the
model gets it (:meth:`SubagentThreadStore.turn_context`): its saved system
prompt and its history, made acceptable to every provider
(:func:`repair_thread_history`). A summary a turn made of its context is kept
for the next turn (:meth:`SubagentThreadStore.persist_context`), with the
owner's own compaction machinery, so the next turn starts from it.

Lifetime: an open thread with nothing running closes after
``THREAD_IDLE_CLOSE_SECONDS``; a conversation keeps at most
``MAX_OPEN_THREADS_PER_CONVERSATION`` open; a closed thread, its conversation
and its raw outputs are removed ``THREAD_RETENTION_SECONDS`` after it closed,
once none of its results is still waiting to be delivered.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# A conversation keeps at most this many subagents open (budget guard: it
# bounds what the agent has to choose from, not how it works).
MAX_OPEN_THREADS_PER_CONVERSATION = 16
# An open thread with no task running for this long closes on its own.
THREAD_IDLE_CLOSE_SECONDS = 7 * 86_400
# A closed thread — its conversation and raw tool outputs — is kept this long.
THREAD_RETENTION_SECONDS = 30 * 86_400

THREAD_STATUSES = ("open", "closed")
# Why a thread closed: agent_close ("agent"), the owner ("owner", no client
# path yet), its conversation deleted ("parent_deleted"), a week idle
# ("idle"), room made for a new one ("cap").
THREAD_CLOSE_REASONS = ("agent", "owner", "parent_deleted", "idle", "cap", "pruned")
# Messages from the main agent a thread with no task running keeps for its
# next task (agent_message to an idle subagent), at most this many.
MAX_PENDING_MAIL = 32
THREAD_KEY_PREFIX = "subagent:"

_THREAD_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Rows a subagent turn carries beyond the tool protocol (saved, projected away
# for the model): what a row is, who wrote it, a tool call's outcome, the run's.
_CARRY_KEYS = ("kind", "author", "status", "outcome", "turn", "noteKind", "_display_hidden")
# What a history row says in place of what a provider would refuse.
STOPPED_REPLY_TEXT = "[This task was stopped before it finished.]"
EMPTY_REPLY_TEXT = "[No reply.]"
UNANSWERED_CALL_TEXT = (
    "No result: the turn that made this call ended before its result was recorded. "
    "It may or may not have run; check before relying on it."
)


def is_thread_id(value: Any) -> bool:
    return isinstance(value, str) and _THREAD_ID_RE.match(value) is not None


def thread_session_key(thread_id: str) -> str:
    return f"{THREAD_KEY_PREFIX}{thread_id}"


@dataclass
class SubagentThreadRecord:
    """The edge from a conversation to one of its subagents."""

    thread_id: str                     # == the first run's run_id
    parent_session_key: str
    child_session_key: str             # "subagent:<thread_id>"
    status: str = "open"               # "open" | "closed"
    created_at: float = 0.0
    closed_at: float | None = None
    closed_reason: str | None = None   # see THREAD_CLOSE_REASONS
    origin: str = "agent"              # "owner" | "agent" (system runs never get a thread)
    title: str = ""
    mark: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    depth: int = 1
    runs: list[str] = field(default_factory=list)   # run_ids, oldest first
    active_run_id: str | None = None   # the turn running now, if any
    last_run_ended_at: float | None = None
    usage: dict[str, int] = field(default_factory=dict)  # summed over ended runs
    revision: int = 0
    updated_at: float | None = None
    # Messages from the main agent while no task ran ({"id", "text", "at"}),
    # oldest first: the next task starts with them.
    pending_mail: list[dict[str, Any]] = field(default_factory=list)


def last_active(thread: SubagentThreadRecord) -> float:
    """When the thread last did something: its last run's end, else its start."""
    return thread.last_run_ended_at or thread.created_at


def decode_thread(item: Any) -> SubagentThreadRecord:
    """A thread record from ``threads.json``; ValueError when it is not one."""
    if not isinstance(item, dict):
        raise ValueError("invalid thread record")
    known = {k: item[k] for k in SubagentThreadRecord.__dataclass_fields__ if k in item}
    try:
        thread = SubagentThreadRecord(**known)
    except TypeError as exc:
        raise ValueError("incomplete thread record") from exc
    if not is_thread_id(thread.thread_id):
        raise ValueError("invalid thread identity")
    if thread.child_session_key != thread_session_key(thread.thread_id):
        raise ValueError("invalid thread conversation")
    if not isinstance(thread.parent_session_key, str) or not thread.parent_session_key:
        raise ValueError("invalid thread parent")
    if thread.status not in THREAD_STATUSES:
        raise ValueError("invalid thread status")
    if thread.closed_reason is not None and thread.closed_reason not in THREAD_CLOSE_REASONS:
        raise ValueError("invalid thread close reason")
    if thread.origin not in ("owner", "agent"):
        raise ValueError("invalid thread origin")
    for name in ("created_at", "closed_at", "last_run_ended_at", "updated_at"):
        value = getattr(thread, name)
        if value is None and name != "created_at":
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid thread timestamp")
    if not isinstance(thread.title, str) or not isinstance(thread.mark, dict):
        raise ValueError("invalid thread label")
    if thread.model is not None and not isinstance(thread.model, str):
        raise ValueError("invalid thread model")
    if type(thread.depth) is not int or thread.depth < 1:
        raise ValueError("invalid thread depth")
    if not isinstance(thread.runs, list) or any(not is_thread_id(r) for r in thread.runs):
        raise ValueError("invalid thread runs")
    if thread.active_run_id is not None and not is_thread_id(thread.active_run_id):
        raise ValueError("invalid thread run")
    if not isinstance(thread.usage, dict) or any(
        not isinstance(k, str) or type(v) is not int or v < 0 for k, v in thread.usage.items()
    ):
        raise ValueError("invalid thread usage")
    if type(thread.revision) is not int or thread.revision < 0:
        raise ValueError("invalid thread revision")
    if not isinstance(thread.pending_mail, list) or len(thread.pending_mail) > MAX_PENDING_MAIL or any(
        not isinstance(m, dict) or not isinstance(m.get("id"), str) or not isinstance(m.get("text"), str)
        for m in thread.pending_mail
    ):
        raise ValueError("invalid thread mail")
    return thread


def usage_view(usage: dict[str, int]) -> dict[str, int]:
    return {
        "inputTokens": int(usage.get("input", 0)),
        "outputTokens": int(usage.get("output", 0)),
        "cacheReadTokens": int(usage.get("cacheRead", 0)),
        "totalTokens": int(usage.get("total", 0)),
    }


def thread_view(thread: SubagentThreadRecord, runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The wire shape of a thread (schemaVersion 1); ``runs`` are its run views, oldest first."""
    return {
        "schemaVersion": 1,
        "threadId": thread.thread_id,
        "childSessionKey": thread.child_session_key,
        "parentSessionKey": thread.parent_session_key,
        "status": thread.status,
        "title": thread.title,
        "mark": dict(thread.mark),
        "model": thread.model,
        "origin": thread.origin,
        "depth": thread.depth,
        "createdAt": thread.created_at,
        "closedAt": thread.closed_at,
        "closedReason": thread.closed_reason,
        "activeRunId": thread.active_run_id,
        "lastRunEndedAt": thread.last_run_ended_at,
        "turns": len(thread.runs),
        "runIds": list(thread.runs),
        "runs": list(runs or []),
        "usage": usage_view(thread.usage),
        "pendingMessages": len(thread.pending_mail),
    }


def _call_id(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    return str(call.get("call_id") or call.get("id") or "")


def _call_name(call: Any) -> str:
    function = call.get("function") if isinstance(call, dict) else None
    return str((function or {}).get("name") or "") if isinstance(function, dict) else ""


def _has_text(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            not isinstance(part, dict) or part.get("type") != "text" or str(part.get("text") or "").strip()
            for part in content
        )
    return content is not None


def repair_thread_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A thread's history made acceptable to every provider.

    A turn that was stopped, or that a crash ended, can leave a reply whose
    calls have no result anywhere in the conversation — not only at its end:
    the turn's closing row and later turns follow. Providers that require
    every call to be answered right after the reply that made it refuse the
    whole request. So:

    * each reply's calls are answered right after it, in the order they were
      made — a result recorded for it is kept, a call with none gets the
      factual :data:`UNANSWERED_CALL_TEXT`;
    * a result that answers no call of the reply before it is dropped;
    * a reply with neither text nor calls says so (:data:`STOPPED_REPLY_TEXT`
      for a stopped turn is set by the projection).

    Returns a new list; a well-formed history comes back equal.
    """
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        role = message.get("role")
        if role == "tool":
            continue  # answers no call of the reply before it
        if role == "assistant" and not message.get("tool_calls") and not _has_text(message.get("content")):
            message = {**message, "content": EMPTY_REPLY_TEXT}
        result.append(message)
        calls = [c for c in (message.get("tool_calls") or []) if isinstance(c, dict)] if role == "assistant" else []
        if not calls:
            continue
        declared = [_call_id(call) for call in calls]
        answers: dict[str, dict[str, Any]] = {}
        while index < len(messages) and messages[index].get("role") == "tool":
            answer = messages[index]
            index += 1
            call_id = str(answer.get("tool_call_id") or "")
            if call_id in declared and call_id not in answers:
                answers[call_id] = answer
        for call, call_id in zip(calls, declared):
            result.append(answers.get(call_id) or {
                "role": "tool", "tool_call_id": call_id, "name": _call_name(call),
                "content": UNANSWERED_CALL_TEXT,
            })
    return result


def thread_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a thread's canonical rows give the model: its history for a later turn.

    The protocol fields only (``role``, ``content``, an assistant's
    ``tool_calls``, a tool result's ``tool_call_id`` and ``name``), the
    summary a turn kept (a ``system`` row), nothing a client alone reads; a
    stopped turn's empty closing row says it was stopped; then
    :func:`repair_thread_history`.
    """
    from flowly.compaction.types import is_summary_message

    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role == "system":
            if is_summary_message(row):
                projected.append({"role": "system", "content": row.get("content", "")})
            continue
        if role not in ("user", "assistant", "tool") or row.get("kind") == "context_boundary":
            continue
        message: dict[str, Any] = {"role": role, "content": row.get("content", "")}
        if role == "assistant" and row.get("tool_calls"):
            message["tool_calls"] = row["tool_calls"]
        elif role == "assistant" and row.get("aborted") and not _has_text(message["content"]):
            message["content"] = STOPPED_REPLY_TEXT
        if role == "tool":
            message["tool_call_id"] = row.get("tool_call_id")
            message["name"] = row.get("name")
        projected.append(message)
    return repair_thread_history(projected)


@dataclass
class TurnContext:
    """What a thread's next turn starts from."""

    system_prompt: str            # the thread's own (saved with its first turn); "" if none was saved
    history: list[dict[str, Any]]  # thread_history of its conversation


def _provider_usage(usage: dict[str, int] | None) -> dict[str, int] | None:
    """A run's token totals in the session's (provider) usage shape."""
    if not usage:
        return None
    return {
        "prompt_tokens": int(usage.get("input", 0)),
        "completion_tokens": int(usage.get("output", 0)),
        "cache_read_tokens": int(usage.get("cacheRead", 0)),
        "total_tokens": int(usage.get("total", 0)),
    }


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


class SubagentThreadStore:
    """Subagent conversations: the SessionManager machinery, in its own directory.

    ``root`` is the registry's directory (``<home>/subagents``). No search
    indexer is attached: a thread is never indexed, listed or exported.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sessions_dir = root / "sessions"
        self.spill_root = root / "spill"
        self._sessions: Any | None = None

    @property
    def sessions(self) -> Any:
        if self._sessions is None:
            from flowly.session.manager import SessionManager

            _private_dir(self.sessions_dir)
            self._sessions = SessionManager(self.root, sessions_dir=self.sessions_dir)
        return self._sessions

    def spill_dir(self, thread_id: str) -> Path:
        """Where the thread's raw tool outputs go (created when first written)."""
        if not is_thread_id(thread_id):
            raise ValueError("invalid thread identity")
        _private_dir(self.spill_root)
        return self.spill_root / thread_id

    def _file_stem(self, thread_id: str) -> str:
        return f"subagent_{thread_id}"

    def has_history(self, thread_id: str) -> bool:
        return is_thread_id(thread_id) and (self.sessions_dir / f"{self._file_stem(thread_id)}.jsonl").is_file()

    def commit_turn(
        self,
        thread: SubagentThreadRecord,
        *,
        run_id: str,
        turn: int,
        trigger: str,
        system_prompt: str,
        first_message: str,
        brief: str,
        journal: list[dict[str, Any]],
        result: str,
        outcome: str,
        usage: dict[str, int] | None,
        duration_ms: int | None,
        aborted: bool,
        with_conversation: bool | None = None,
    ) -> bool:
        """Save one finished run as a turn of the thread's conversation.

        The turn is what the provider saw: the task row (the brief, with the
        forked conversation on the first turn), every assistant reply with its
        tool calls, every tool message as the model got it (a long one ends
        with a pointer to its full output in the thread's spill dir), the main
        agent's notes as they were read, and a closing row with the run's
        result, outcome, tokens and duration. Rows the model saw but no one
        is shown (the summary request) are ``_display_hidden``.

        Idempotent per run: a run already saved (a restart that recorded the
        result again) is not saved twice. Returns whether it saved.
        """
        key = thread.child_session_key
        rows: list[dict[str, Any]] = []
        for message in journal:
            row = dict(message)
            row.setdefault("run_id", run_id)
            rows.append(row)
        rows.append({"role": "assistant", "content": result or "", "outcome": outcome, "turn": turn})
        user_extras: dict[str, Any] = {
            "kind": "subagent_task", "author": "main_agent", "run_id": run_id,
            "turn": turn, "trigger": trigger,
        }
        if first_message != brief:
            # The first turn carries the conversation it was given, a later
            # one the label (and the messages it found waiting); the brief
            # alone is what a client heads the task with.
            user_extras["brief"] = brief
            if with_conversation if with_conversation is not None else turn == 1:
                user_extras["with_conversation"] = True

        def write(session: Any) -> bool:
            meta = session.metadata.get("subagent")
            if not isinstance(meta, dict):
                meta = {
                    "threadId": thread.thread_id,
                    "parentSessionKey": thread.parent_session_key,
                    "origin": thread.origin,
                    "title": thread.title,
                    "mark": dict(thread.mark),
                    "model": thread.model,
                    "depth": thread.depth,
                    "createdAt": thread.created_at,
                    # Later turns continue with the same prompt (it depends on
                    # the first brief, unlike the main agent's).
                    "systemPrompt": system_prompt,
                }
                session.metadata["subagent"] = meta
            elif system_prompt and not meta.get("systemPrompt"):
                # The first turn ended without its prompt (a restart that
                # could not continue it); later turns keep this one.
                meta["systemPrompt"] = system_prompt
            committed = meta.get("runs")
            if not isinstance(committed, list):
                committed = meta["runs"] = []
            if run_id in committed:
                return False
            session.extend_with_turn_messages(
                user_content=first_message,
                new_messages=rows,
                final_content=result or "",
                usage=_provider_usage(usage),
                aborted=aborted,
                duration_ms=duration_ms,
                run_id=run_id,
                user_extras=user_extras,
                carry_keys=_CARRY_KEYS,
            )
            committed.append(run_id)
            return True

        sessions = self.sessions
        try:
            saved = bool(sessions.mutate(key, write))
        finally:
            # A thread is read rarely; its rows never stay in memory.
            sessions.evict(key)
        self._make_private(thread.thread_id)
        return saved

    def turn_context(self, thread_id: str) -> TurnContext:
        """The thread's saved system prompt and its history as its next turn's model gets it.

        Never creates the conversation; a thread with none (its first turn was
        never saved) has no history.
        """
        if not self.has_history(thread_id):
            return TurnContext("", [])
        sessions = self.sessions
        session = sessions.read(thread_session_key(thread_id))
        if session is None:
            return TurnContext("", [])
        meta = session.metadata.get("subagent")
        prompt = meta.get("systemPrompt") if isinstance(meta, dict) else None
        return TurnContext(prompt if isinstance(prompt, str) else "", thread_history(session.messages))

    def persist_context(self, thread: SubagentThreadRecord, *, run_id: str,
                        summary: dict[str, Any], kept: list[dict[str, Any]]) -> bool:
        """Keep the summary a turn made of its context for the thread's next turn.

        Called once the turn is saved. The thread's working context becomes
        ``[summary, *kept]`` — ``kept`` the messages the model had after the
        summary at the turn's end (its task, what it did since, its closing
        reply) — through the owner's own compaction machinery: the rows the
        summary replaced are marked compacted in the append-only display log
        (which still shows every row), the summary and any kept message that
        is not a saved row are archived hidden, and the display gets the
        compaction seam. Idempotent per run (``meta.contextRun``). Returns
        whether it changed the conversation.
        """
        from flowly.compaction.types import SUMMARY_METADATA_KEY, extract_summary_text
        from flowly.session.archive import (
            ARCHIVE_SUMMARY_KEY,
            ARCHIVE_TRANSACTION_KEY,
            EVENT_ID_KEY,
            SUMMARY_COVERS_KEY,
            match_kept_events,
        )

        key = thread.child_session_key
        sessions = self.sessions
        compaction_id = f"cmp_{run_id.replace('-', '')[:12]}"
        prepared: dict[str, str] = {}

        def swap(session: Any) -> bool:
            meta = session.metadata.get("subagent")
            if not isinstance(meta, dict) or run_id not in (meta.get("runs") or []):
                return False  # the turn itself must be saved first
            if meta.get("contextRun") == run_id:
                return False
            source = list(session.messages)
            identities, covers, transaction = sessions.prepare_compaction_archive(
                session, kept, source_message_count=len(source), compaction_id=compaction_id,
            )
            by_id = {str(row.get(EVENT_ID_KEY)): row for row in source if row.get(EVENT_ID_KEY)}
            session.clear()
            session.add_message("system", str(summary.get("content") or ""), **{
                SUMMARY_METADATA_KEY: True, ARCHIVE_SUMMARY_KEY: True, "_display_hidden": True,
                SUMMARY_COVERS_KEY: covers, ARCHIVE_TRANSACTION_KEY: transaction,
            })
            sessions.append_compaction_summary(session, dict(session.messages[-1]))
            matches = match_kept_events(source, kept)
            for message, identity in zip(kept, matches):
                row = by_id.get(str((identity or {}).get(EVENT_ID_KEY) or ""))
                if row is not None:
                    # The saved row itself: uncut, with what clients read of it.
                    session.messages.append(dict(row))
                    continue
                extras = {k: message[k] for k in ("tool_calls", "tool_call_id", "name") if k in message}
                session.add_message(message.get("role", "user"), message.get("content", ""),
                                    _display_hidden=True, _archive_derived_copy=True, **extras)
                sessions.append_compaction_summary(session, dict(session.messages[-1]))
            sessions.mark_full_synced(session)
            session.metadata["last_compaction_summary"] = extract_summary_text(summary) or ""
            session.metadata["last_compaction_coverage"] = covers
            session.metadata["compaction_count"] = int(session.metadata.get("compaction_count", 0) or 0) + 1
            session.metadata["_pending_archive_transaction"] = transaction
            sessions.prepare_context_boundary(session, compaction_id)
            meta["contextRun"] = run_id
            prepared["transaction"] = transaction
            return True

        def finish(session: Any) -> None:
            sessions.finalize_compaction_archive(session, prepared["transaction"])
            sessions.finalize_context_boundary(session, compaction_id)

        try:
            changed = bool(sessions.mutate(key, swap))
            if changed:
                # Phase two; a crash before it is finished when the
                # conversation is next loaded (the markers are saved).
                sessions.mutate(key, finish)
        finally:
            sessions.evict(key)
        self._make_private(thread.thread_id)
        return changed

    def _make_private(self, thread_id: str) -> None:
        if os.name != "posix":
            return
        stem = self._file_stem(thread_id)
        for name in (f"{stem}.jsonl", f"{stem}.full.jsonl"):
            try:
                os.chmod(self.sessions_dir / name, 0o600)
            except OSError:
                pass

    def display_rows(self, thread_id: str) -> list[dict[str, Any]]:
        """The thread's whole display history, oldest first; never creates it."""
        if not self.has_history(thread_id):
            return []
        sessions = self.sessions
        key = thread_session_key(thread_id)
        try:
            return sessions.get_full_messages(key)
        finally:
            sessions.evict(key)

    def committed_runs(self, thread_id: str) -> list[str]:
        """Runs the thread's conversation has saved, oldest first."""
        if not self.has_history(thread_id):
            return []
        session = self.sessions.read(thread_session_key(thread_id))
        meta = (session.metadata.get("subagent") if session is not None else None) or {}
        runs = meta.get("runs")
        return [r for r in runs if isinstance(r, str)] if isinstance(runs, list) else []

    def delete(self, thread_id: str) -> None:
        """Remove a thread's conversation and raw outputs."""
        if not is_thread_id(thread_id):
            return
        if self.sessions_dir.exists():
            try:
                self.sessions.delete(thread_session_key(thread_id))
            except Exception:
                logger.exception("Could not delete a subagent conversation")
            self._unlink_lock(thread_id)
        self._remove_spill(thread_id)

    def _unlink_lock(self, thread_id: str) -> None:
        try:
            (self.sessions_dir / ".locks" / f"{self._file_stem(thread_id)}.lock").unlink(missing_ok=True)
        except OSError:
            pass

    def _remove_spill(self, thread_id: str) -> None:
        path = self.spill_root / thread_id
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            logger.warning("Could not remove a subagent's spilled tool outputs")

    def prune(self, keep: set[str]) -> None:
        """Remove every thread's files but those of ``keep`` (thread ids).

        Only our own names are touched: ``subagent_<uuid>[.full].jsonl`` and
        its lock, and ``spill/<uuid>/``.
        """
        pattern = re.compile(r"^subagent_([0-9a-f-]{36})(?:\.full)?\.jsonl$")
        if self.sessions_dir.is_dir():
            for path in self.sessions_dir.iterdir():
                match = pattern.match(path.name)
                if match and is_thread_id(match.group(1)) and match.group(1) not in keep:
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Could not prune an expired subagent conversation")
                    self._unlink_lock(match.group(1))
        if self.spill_root.is_dir():
            for path in self.spill_root.iterdir():
                if is_thread_id(path.name) and path.name not in keep:
                    self._remove_spill(path.name)

