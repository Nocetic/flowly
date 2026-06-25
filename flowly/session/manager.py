"""Session management for conversation history."""

import json
import os
import secrets
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from flowly.utils.helpers import ensure_dir, safe_filename
from flowly.profile import get_flowly_home


# Suffix of the append-only DISPLAY transcript that rides alongside each
# canonical ``<key>.jsonl``. It shares the ``*.jsonl`` glob, so EVERY consumer
# that lists session files must skip it — otherwise each session surfaces a
# phantom ``<key>.full`` twin (seen as a clone of a streaming chat).
FULL_TRANSCRIPT_SUFFIX = ".full.jsonl"


def iter_session_files(sessions_dir: Path) -> Iterator[Path]:
    """Yield the CANONICAL ``<key>.jsonl`` session files in ``sessions_dir``,
    skipping the ``<key>.full.jsonl`` display mirrors.

    The single place this rule lives — every session-file consumer (listing,
    RPC payloads, search indexing, …) should iterate through here instead of
    globbing ``*.jsonl`` directly, so a new consumer can't reintroduce the
    phantom-twin bug.
    """
    for path in sorted(sessions_dir.glob("*.jsonl")):
        if path.name.endswith(FULL_TRANSCRIPT_SUFFIX):
            continue
        yield path

# Maximum number of sessions to keep in memory cache (LRU eviction)
_MAX_CACHED_SESSIONS = 200


# Fields preserved by ``_project_for_llm`` when returning a message
# from the session store back to the agent loop. Anything not in this
# set is internal bookkeeping (timestamps, audit flags, etc.) and is
# stripped before the message goes to the LLM. The list mirrors the
# OpenAI / Anthropic chat-completions message shape so providers don't
# reject the payload for unrecognised fields.
_LLM_BASE_FIELDS: tuple[str, ...] = ("role", "content")
_LLM_ROLE_FIELDS: dict[str, tuple[str, ...]] = {
    "assistant": ("tool_calls",),
    "tool": ("tool_call_id", "name"),
}

# Numeric usage fields we persist on assistant messages. Limited to
# JSON-safe ints so the JSONL line stays cheap to parse and downstream
# consumers (gateway chat.history RPC, list_sessions metadata, future
# cost dashboard) don't have to defensively coerce arbitrary types.
_USAGE_FIELDS: tuple[str, ...] = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens",
    "input_tokens", "output_tokens",
)


def _filter_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Coerce raw usage dict to the persisted numeric subset.

    The agent loop builds ``total_usage`` from provider responses and
    those dicts can ride along with stringy values (cost estimates,
    debug fields) we don't want on disk. Persisting a filtered copy
    keeps the JSONL line tight and means readers can trust every value
    is an int. Empty / non-numeric entries are dropped silently — they
    add nothing the hydrator can use anyway.
    """
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    for k in _USAGE_FIELDS:
        v = usage.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v:
            out[k] = int(v)
    return out


def _project_for_llm(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``msg`` containing only LLM-protocol fields.

    Why a separate projection step
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Session messages are stored verbatim on disk (timestamps, audit
    flags, future per-message metadata can all ride along without
    disrupting consumers). The LLM only wants the protocol-shaped
    subset, so this function does the narrowing in one place. The
    role-specific allowlist (``_LLM_ROLE_FIELDS``) is conservative
    on purpose — adding a new field to it should be a deliberate
    decision, not an accidental leak of internal state into provider
    requests.

    ``content`` is kept as-is whether it's a ``str`` or a
    ``list[dict]`` (the latter is how multimodal tool results — text +
    image — reach the model; OpenAI and Anthropic both accept that
    shape).
    """
    out: dict[str, Any] = {}
    for k in _LLM_BASE_FIELDS:
        if k in msg:
            out[k] = msg[k]
    extras = _LLM_ROLE_FIELDS.get(msg.get("role", ""), ())
    for k in extras:
        if k in msg:
            out[k] = msg[k]
    return out


def _repair_tool_sequence(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trim orphan tool_calls / tool results from the tail.

    Provider chat-completions APIs require strict alternation:
    every assistant message with ``tool_calls`` must be followed by
    a ``tool`` message for each ``tool_call_id`` it issued, before
    the next user/assistant message. If a turn crashed mid-tool
    execution (process killed, OOM, network reset between Codex
    spawning the call and the subprocess returning), the on-disk
    session may end with either:

      * An assistant_with_tool_calls but NO tool results after it.
      * One or more tool messages whose assistant_with_tool_calls
        partner is missing earlier ids (e.g. half of a 2-call batch
        landed before the crash).

    Either case makes the next ``LLM.chat()`` call return a 400
    (OpenAI: ``messages with tool_calls must be followed by tool
    messages``). Repairing here keeps a crashed session resumable:
    we lose a partial turn from the LLM's perspective but the user
    can simply re-ask, which is far better than the conversation
    becoming irrecoverable.

    Repair walks ONLY the tail — earlier orphans (if any made it
    past compaction) are preserved so an audit log keeps the full
    history. Walking from the end is also cheaper than a full pass
    on long conversations.

    Returns a new list; the input is not mutated.
    """
    result = list(messages)
    while result:
        last = result[-1]
        role = last.get("role")

        # Pattern 1: trailing assistant with tool_calls but no following
        # tool messages → drop the orphan assistant.
        if role == "assistant" and last.get("tool_calls"):
            # If we're at the end and there are no tool messages after,
            # this assistant is begging for tool results that never came.
            result.pop()
            continue

        # Pattern 2: trailing tool messages whose triggering
        # assistant_with_tool_calls has unmet ids. We collect all
        # contiguous trailing tool messages, find the assistant before
        # them, and verify each declared tool_call_id has a matching
        # tool reply. If any id is missing, drop the entire half-
        # finished batch (tool tail + the assistant).
        if role == "tool":
            tail_tools: list[dict[str, Any]] = []
            idx = len(result) - 1
            while idx >= 0 and result[idx].get("role") == "tool":
                tail_tools.append(result[idx])
                idx -= 1
            if idx < 0 or result[idx].get("role") != "assistant":
                # Tool messages with no preceding assistant —
                # truly orphan, drop them.
                result = result[: idx + 1]
                continue
            issuing = result[idx]
            declared_ids = {
                tc.get("id")
                for tc in (issuing.get("tool_calls") or [])
                if isinstance(tc, dict)
            }
            satisfied_ids = {t.get("tool_call_id") for t in tail_tools}
            if declared_ids and not (declared_ids - satisfied_ids):
                # Complete: every declared id has a matching tool reply.
                # Sequence is valid, stop trimming.
                break
            # Incomplete: at least one declared id never got a result.
            # Drop the tail tools + the issuing assistant so the
            # remaining history ends on a clean (user / assistant text)
            # boundary the provider will accept.
            result = result[:idx]
            continue

        # Anything else (user, system, plain assistant) is a clean tail.
        break

    return result


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.

        Preserves OpenAI/Anthropic tool-protocol fields so cross-turn
        tool reasoning works:

          * ``assistant`` messages keep ``tool_calls`` (if any).
          * ``tool`` messages keep ``tool_call_id`` and ``name`` —
            both are required by the providers' chat-completions API
            for matching a tool result to its triggering call.

        Other persisted fields (timestamps, internal flags, extra
        kwargs) are stripped — only the LLM needs the bare protocol
        shape. Without this preservation the LLM would see prior
        assistant turns as pure-text monologues, losing every tool
        call + result from prior conversation steps.

        The returned list also runs through
        :func:`_repair_tool_sequence` so any orphaned tool_calls
        (assistant_with_tool_calls without matching tool result, or
        tool messages without their triggering assistant) get trimmed
        off the tail. Provider APIs reject malformed sequences with
        a 400 — repairing defensively here means a crashed-mid-turn
        session can resume cleanly without users hitting opaque
        provider errors.

        Args:
            max_messages: Maximum messages to return.

        Returns:
            List of messages in LLM-protocol shape.
        """
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        projected = [_project_for_llm(m) for m in recent]
        return _repair_tool_sequence(projected)

    def extend_with_turn_messages(
        self,
        *,
        user_content: str,
        new_messages: list[dict[str, Any]],
        final_content: str | None,
        usage: dict[str, Any] | None = None,
        media: list[str] | None = None,
        reply_media: list[str] | None = None,
        user_display_hidden: bool = False,
    ) -> None:
        """Append a completed turn — user message + all assistant/tool
        messages the loop produced — to the session.

        Why this lives on Session
        ~~~~~~~~~~~~~~~~~~~~~~~~~

        Every code path that drives a turn through ``AgentLoop._run_llm_tool_loop``
        (main user message, system / subagent announce, cron jobs, …)
        ends with the same persistence pattern: snapshot the messages
        the loop appended, save the user prompt, then save each loop
        message preserving its tool-protocol fields. Centralising that
        here means the four-step recipe only has to be right once;
        future call sites get the correct ChatGPT-style full-structure
        persistence for free.

        Parameters
        ----------
        user_content:
            The user's prompt for this turn. Saved as the first new
            message. Caller is responsible for any prefixing (e.g.
            ``[System: announcer]`` markers in the system-message
            path).
        new_messages:
            The slice of the loop's ``messages`` list that was added
            during this turn — typically ``messages[turn_start_idx:]``
            where ``turn_start_idx`` was captured before the loop ran.
            Each entry's ``tool_calls``, ``tool_call_id``, and
            ``name`` fields are carried through so cross-turn tool
            reasoning works.
        final_content:
            The post-processed closing assistant text (after voice
            sanitisation, error-fallback synthesis, etc.). May differ
            from the last ``new_messages`` entry's ``content`` — in
            that case the closing assistant message is overridden so
            the saved transcript matches what the user actually
            received. ``None`` is treated as empty.
        usage:
            Aggregate token usage for this turn (prompt/completion/
            cache_read/cache_write/total). Attached to the closing
            assistant message so the resume hydrator can rebuild the
            context-window indicator without re-running the LLM. Also
            accumulated into ``session.metadata['token_totals']`` for
            cheap session-wide queries. ``None`` skips persistence.
        """
        # Persist the media file paths alongside the user message so chat
        # history can reconstruct attachment previews (the direct gateway has
        # no Firestore tool_turns/ — the session jsonl is the source of truth).
        # Projected away for the LLM by ``_project_for_llm`` (extra field).
        # ``user_display_hidden`` marks an internal trigger (e.g. a subagent
        # completion announce) so it stays in the LLM context — the agent needs
        # it to produce its summary — but never reaches the user-facing display
        # transcript as a "user" message. The ``_``-prefixed key is stripped
        # before the provider (get_history / _strip_internal_keys), so the LLM
        # sees exactly what it saw before; only flush_full skips it.
        user_extras: dict[str, Any] = {}
        if media:
            user_extras["media"] = list(media)
        if user_display_hidden:
            user_extras["_display_hidden"] = True
        self.add_message("user", user_content, **user_extras)

        # Find the index of the closing plain-text assistant message,
        # if any. Only this message's content is overridden by
        # ``final_content`` — tool-call assistant messages have their
        # own preamble text that lives alongside the tool calls and
        # shouldn't be clobbered.
        closing_idx: int | None = None
        for i in range(len(new_messages) - 1, -1, -1):
            m = new_messages[i]
            if m.get("role") == "assistant" and not m.get("tool_calls"):
                closing_idx = i
                break

        clean_usage = _filter_usage(usage)

        for i, new_msg in enumerate(new_messages):
            extras = {
                k: new_msg[k]
                for k in ("tool_calls", "tool_call_id", "name")
                if k in new_msg
            }
            # Attach the turn-level usage to the closing plain-text
            # assistant message only. Mid-turn tool-call assistants
            # don't carry usage in any provider's response shape;
            # giving them an aggregate would mislead the hydrator.
            if i == closing_idx and clean_usage:
                extras["usage"] = clean_usage
            # Media the agent PRODUCED this turn (image_generate / screenshot)
            # rides the closing assistant message — so chat.history reconstructs
            # the image preview on the assistant bubble, same as the live reply.
            if i == closing_idx and reply_media:
                extras["media"] = list(reply_media)
            content = new_msg.get("content") or ""
            if i == closing_idx and final_content:
                content = final_content
            self.add_message(
                new_msg.get("role", "assistant"),
                content,
                **extras,
            )

        # Loop ended without a plain-text closing assistant but the
        # caller still produced a final_content (synthesised fallback
        # like "Action executed.", error string, etc.). Append it as
        # a capstone so the saved transcript ends on a clean assistant
        # text boundary the next turn can extend cleanly.
        if closing_idx is None and (final_content or reply_media):
            extras = {"usage": clean_usage} if clean_usage else {}
            if reply_media:
                extras["media"] = list(reply_media)
            self.add_message("assistant", final_content or "", **extras)

        # Roll the turn's usage into session-wide totals so list_sessions
        # / future cost dashboards can read aggregates without scanning
        # every message line. Stored under metadata so it survives
        # save/load via the existing metadata serialisation — no schema
        # bump, no compatibility break for older readers.
        if clean_usage:
            totals = self.metadata.setdefault("token_totals", {})
            for k in (
                "prompt_tokens", "completion_tokens",
                "cache_read_tokens", "cache_write_tokens",
                "reasoning_tokens", "total_tokens",
            ):
                v = clean_usage.get(k)
                if isinstance(v, (int, float)) and v:
                    totals[k] = totals.get(k, 0) + int(v)
            totals["turn_count"] = totals.get("turn_count", 0) + 1
            self.metadata["last_turn_usage"] = clean_usage

    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()

    def drop_last_assistant_chain(self) -> str | None:
        """Remove trailing assistant + tool messages; return last user text.

        Used by ``/retry``: the user wants to re-ask their most recent
        message and get a fresh assistant reply. We strip everything
        from the tail back to (but not including) the last user
        message, then return that user's content so the caller can
        re-submit it via ``chat.send``.

        Returns ``None`` when there's nothing sensible to retry — empty
        session, or session ending on a user message (no assistant reply
        yet to drop). The caller should surface that as a no-op.
        """
        if not self.messages:
            return None
        # Walk back over assistant + tool tail.
        idx = len(self.messages) - 1
        while idx >= 0 and self.messages[idx].get("role") in ("assistant", "tool"):
            idx -= 1
        # idx now points at the last non-(assistant/tool) message.
        if idx < 0 or self.messages[idx].get("role") != "user":
            # Either nothing left, or the trailing run wasn't preceded
            # by a user message — no clear "last prompt" to retry.
            return None
        # If the tail had no assistant/tool messages there's nothing
        # to drop (session already ends on a user turn). Nothing was
        # mutated, and the user can just resubmit by hand — we still
        # return their text so the slash handler can decide.
        if idx == len(self.messages) - 1:
            return self._extract_text(self.messages[idx])
        # Drop the trailing chain.
        self.messages = self.messages[: idx + 1]
        self.updated_at = datetime.now()
        return self._extract_text(self.messages[idx])

    def drop_last_turn(self) -> str | None:
        """Remove the last user message and everything after it.

        Used by ``/undo``: the user regrets their most recent prompt
        (and whatever the assistant produced from it). The complete
        ``user → assistant [→ tool ...]`` slice gets popped. Returns
        the removed user text so the caller can optionally pre-fill
        the composer for an "edit and resubmit" flow.

        Returns ``None`` when there's no user turn to undo — empty
        session or system-only history.
        """
        if not self.messages:
            return None
        # Find the index of the LAST user message.
        last_user_idx: int | None = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            return None
        removed_text = self._extract_text(self.messages[last_user_idx])
        self.messages = self.messages[:last_user_idx]
        self.updated_at = datetime.now()
        return removed_text

    @staticmethod
    def _extract_text(msg: dict[str, Any]) -> str:
        """Best-effort plain-text extraction from a message record.

        Content can be a ``str`` (most messages) or a ``list[dict]``
        (multimodal — text + image). Both /retry and /undo only need
        the textual part to round-trip through the composer.
        """
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if text:
                        parts.append(text)
            return "\n".join(parts)
        return str(content) if content is not None else ""


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    Uses LRU cache to limit memory usage.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(get_flowly_home() / "sessions")
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._indexer: Any | None = None  # lazy-set by AgentLoop

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_full_path(self, key: str) -> Path:
        """Append-only DISPLAY transcript path (``<key>.full.jsonl``).

        The canonical ``<key>.jsonl`` is the LLM *working context* — compaction
        rewrites it as ``[summary] + recent`` to fit the window, which destroys
        the early turns. This second file is an append-only log of every real
        message ever shown, so the chat UI (``chat.history``) can render the full
        conversation regardless of compaction. (The append-only log stays
        separate from the compressed per-turn working context.)
        """
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.full.jsonl"

    # -- Display transcript (append-only) -----------------------------------

    _FULL_WATERMARK_KEY = "_full_log_count"

    def flush_full(self, session: "Session") -> None:
        """Mirror any not-yet-persisted tail of ``session.messages`` into the
        append-only display log. Idempotent via a per-session watermark stored in
        metadata (which survives ``Session.clear()``). Best-effort: a failure
        here never blocks the canonical save."""
        try:
            mark = int(session.metadata.get(self._FULL_WATERMARK_KEY, 0))
        except (TypeError, ValueError):
            mark = 0
        total = len(session.messages)
        if mark >= total:
            session.metadata[self._FULL_WATERMARK_KEY] = total
            return
        new = session.messages[mark:]
        try:
            path = self._get_full_path(session.key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                for msg in new:
                    # Internal triggers (subagent/board/memory announces) live in
                    # the LLM context but must never surface in the user-facing
                    # display transcript as a "user" message. Skip on write; the
                    # watermark still advances to ``total`` so they're never
                    # reconsidered.
                    if msg.get("_display_hidden"):
                        continue
                    f.write(json.dumps(msg) + "\n")
            session.metadata[self._FULL_WATERMARK_KEY] = total
        except Exception as e:  # pragma: no cover - disk best-effort
            logger.debug("Display-log flush failed for %s: %s", session.key, e)

    def mark_full_synced(self, session: "Session") -> None:
        """Declare the current ``session.messages`` as already represented in the
        display log WITHOUT appending — used right after compaction rebuilds the
        context as ``[summary] + kept`` (the kept turns are already in the log and
        the summary must never appear in it). The next real message then appends
        correctly."""
        session.metadata[self._FULL_WATERMARK_KEY] = len(session.messages)

    def get_full_messages(self, key: str) -> list[dict[str, Any]]:
        """The full display transcript for a session — every real message, in
        order, unaffected by compaction. Falls back to the live (possibly
        compacted) ``session.messages`` for sessions that predate the display log
        (their early history is already gone and unrecoverable)."""
        path = self._get_full_path(key)
        if path.exists():
            out: list[dict[str, Any]] = []
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            isinstance(d, dict)
                            and d.get("_type") != "metadata"
                            and not d.get("_display_hidden")
                        ):
                            out.append(d)
                if out:
                    return out
            except Exception as e:  # pragma: no cover
                logger.debug("Display-log read failed for %s: %s", key, e)
        # Fallback for sessions predating the display log: filter internal
        # triggers the same way flush_full does.
        return [
            m for m in self.get_or_create(key).messages
            if not m.get("_display_hidden")
        ]

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        # Check cache (and move to end for LRU)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        # Try to load from disk
        session = self._load(key)
        if session is None:
            session = Session(key=key)

        # Add to cache with LRU eviction
        self._cache[key] = session
        if len(self._cache) > _MAX_CACHED_SESSIONS:
            self._cache.popitem(last=False)  # Remove oldest

        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk with robust error handling."""
        path = self._get_session_path(key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            corrupt_lines = 0

            with open(path, encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        corrupt_lines += 1
                        if corrupt_lines <= 3:
                            logger.warning(f"Skipped corrupt line {line_num} in session {key}")
                        if corrupt_lines > 50:
                            logger.error(f"Too many corrupt lines in session {key}, aborting load")
                            return None
                        continue

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at_str = data.get("created_at")
                        if created_at_str:
                            try:
                                created_at = datetime.fromisoformat(created_at_str)
                            except (ValueError, TypeError):
                                pass
                    else:
                        messages.append(data)

            if corrupt_lines:
                logger.warning(f"Session {key}: loaded with {corrupt_lines} corrupt line(s) skipped")

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata
            )
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}")
            return None

    def save(self, session: Session, extra_messages: list[dict[str, Any]] | None = None) -> None:
        """Save a session to disk atomically.

        ``extra_messages`` are written to the jsonl AFTER ``session.messages``
        but are NOT part of the in-memory session. This lets a turn persist a
        *pending* user message at turn start — so poll-based clients (the
        direct-gateway inbox) and mid-turn re-entries see it before the
        canonical full-turn save — without polluting the history the agent
        builds its prompt from (the user prompt is added separately by the
        loop). The final ``save(session)`` at turn end omits the extra and
        rewrites the file canonically.
        """
        path = self._get_session_path(session.key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first, then atomic rename
        tmp_path = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                # Write metadata first
                metadata_line = {
                    "_type": "metadata",
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata
                }
                f.write(json.dumps(metadata_line) + "\n")

                # Write messages
                for msg in session.messages:
                    f.write(json.dumps(msg) + "\n")

                # Pending (not-yet-in-history) messages — disk only.
                for msg in (extra_messages or []):
                    f.write(json.dumps(msg) + "\n")

            # Atomic rename (POSIX guarantees this is atomic on same filesystem)
            os.replace(str(tmp_path), str(path))
        except Exception:
            # Clean up temp file on failure
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        # Mirror the new tail into the append-only display transcript so the UI
        # keeps the full conversation even after the context jsonl is compacted.
        self.flush_full(session)

        # Update cache
        self._cache[session.key] = session
        if session.key in self._cache:
            self._cache.move_to_end(session.key)

        # Update search index (best-effort, never blocks save)
        if self._indexer is not None:
            try:
                self._indexer.index_session(session.key, session.messages)
            except Exception as e:
                logger.debug("Session index update failed: %s", e)

    def delete(self, key: str) -> bool:
        """
        Delete a session.

        Args:
            key: Session key.

        Returns:
            True if deleted, False if not found.
        """
        # Remove from cache
        self._cache.pop(key, None)

        # Remove file
        path = self._get_session_path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in iter_session_files(self.sessions_dir):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                # Auto-generated descriptive title (see
                                # flowly/session/title.py); None until the first
                                # exchange is titled. Clients fall back to the
                                # key suffix when absent.
                                "title": (data.get("metadata") or {}).get("title"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
