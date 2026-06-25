"""Project Codex item-stream events into Flowly's message format.

Codex emits a turn as a sequence of ``item`` events over JSON-RPC:

  * ``item/started``    — a new atomic output begins (message, tool call, …)
  * ``item/<type>/delta`` — incremental content for an open item
  * ``item/completed``  — item finalised; payload contains the final state

This module owns the translation from those raw event dicts into the
standard Flowly message shape that ``ContextBuilder`` / ``Session`` /
the desktop renderer all already understand:

  * ``{role: "assistant", content: str}``
  * ``{role: "assistant", content: str, tool_calls: [...]}``
  * ``{role: "tool", tool_call_id: str, name: str, content: str}``

Why a separate projection layer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Putting this logic inline in the session loop would entangle two
concerns that change for different reasons. The transport (Phase A)
deals with bytes and JSON-RPC framing; the session (Phase B2) deals
with turn lifecycle, OAuth refresh, wedge detection. Item-shape
translation is its own thing — Codex adds new item types, message
schemas evolve, and a focused module here keeps those edits
isolated.

It also keeps the projector cheaply testable: feed it a sequence of
captured notification dicts, assert the resulting messages match a
golden. No subprocess, no asyncio, no JSON-RPC — just dict-in,
list-out.

Item type catalog
~~~~~~~~~~~~~~~~~

Based on Codex's published item schema (codex-rs/app-server protocol
docs, May 2026):

  ============= ============ ===========================================
  Type          User-visible Projection
  ============= ============ ===========================================
  agentMessage  YES          ``{role: "assistant", content}``
  userMessage   YES (replay) ``{role: "user", content}``
  reasoning     NO           stashed as encrypted_content for continuity
  commandExec.. YES          assistant ``tool_calls=[exec]`` + tool result
  fileChange    YES          assistant ``tool_calls=[apply_patch]`` + result
  mcpToolCall   YES          assistant ``tool_calls=[mcp.X.Y]`` + result
  webSearchCall YES          assistant ``tool_calls=[web_search]`` + result
  dynamicTool.. YES          assistant ``tool_calls=[<name>]`` + result
  ============= ============ ===========================================

Unknown / future item types log a warning and are skipped — the
projector errs on the side of degraded output rather than crashing
mid-turn.

Reasoning continuity
~~~~~~~~~~~~~~~~~~~~

``reasoning`` items carry an ``encrypted_content`` blob — the model's
internal scratchpad, encrypted by Codex for replay on subsequent
turns. They are NOT shown to the user (they're noise to a human),
but the session layer must capture them and ship them back to Codex
in the next ``turn/start`` so the model can "remember" what it was
thinking. The projector collects them into ``reasoning_items`` for
the session to read at turn end.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TurnProjection:
    """Result of projecting one Codex turn into Flowly's world.

    Returned by :meth:`CodexEventProjector.finalize_turn` once
    ``turn/completed`` is observed (or the session decides the turn
    is dead and bails out early).

    Fields:
        messages: Flat list of Flowly-shape messages to append to the
            session. Includes assistant messages, tool_call assistant
            messages, and matching ``role: tool`` results.
        final_text: The concatenated text of the last assistant
            message in the turn. Used as the "final response" the
            wrapping ``codex_session`` tool returns to the parent
            agent. Empty string if the turn produced only tool calls
            and no closing assistant text.
        reasoning_items: Encrypted-content blobs collected from
            ``reasoning`` items during the turn. Stored on the
            session metadata so the next ``turn/start`` can ship
            them back (Codex reads them to preserve thinking state
            across turns).
        tool_iterations: Number of mutating-style items observed
            (commandExecution, fileChange, mcpToolCall,
            dynamicToolCall). Drives Flowly's skill-nudge cadence
            so heavy Codex turns count toward the same heuristic
            native Flowly turns use.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    tool_iterations: int = 0


StreamCallback = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Internal item buffers
# ---------------------------------------------------------------------------


@dataclass
class _OpenItem:
    """Buffered state for an item that's mid-stream.

    Codex sends a turn as ``item/started`` → ``item/<type>/delta`` (×N)
    → ``item/completed``. We accumulate the deltas keyed by
    ``itemId`` so the ``completed`` event has everything it needs to
    finalize the projection.
    """

    item_id: str
    item_type: str
    # Free-form payload bag. Different item types use different
    # fields; keeping this generic lets us tolerate Codex schema
    # additions without code changes (extra fields just get carried
    # through verbatim).
    text_parts: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    # For tool-style items (commandExecution, fileChange, mcpToolCall),
    # the "request" half (what the agent asked for) and the "response"
    # half (what came back) arrive in the same stream. We accumulate
    # both into the same _OpenItem.
    tool_output_parts: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Item type classification
# ---------------------------------------------------------------------------

# Mutating tool-style items — these count toward
# ``tool_iterations`` for the skill-nudge counter (same heuristic
# Flowly's native loop uses). Read-only items (reasoning, agentMessage)
# don't count.
_MUTATING_ITEM_TYPES: frozenset[str] = frozenset({
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "webSearchCall",  # not mutating per se, but spends an iteration
})

# Items that produce a tool_call → tool_result message pair in the
# projection. Maps each Codex item type to the Flowly tool name we
# expose it as. The tool name is what the user sees in the chat UI
# (the ``🔧 exec`` icon, the ``🔧 apply_patch`` icon, etc.) — pick
# names that match Flowly's existing tool naming for visual
# consistency.
_TOOL_LIKE_ITEM_TYPES: dict[str, str] = {
    "commandExecution": "exec",
    "fileChange": "apply_patch",
    "webSearchCall": "web_search",
    # mcpToolCall is special — the tool name is dynamic
    # (server.tool format); see _project_mcp_tool_call().
    # dynamicToolCall is special — the tool name comes from the
    # payload; see _project_dynamic_tool_call().
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_tool_call_id(item_type: str, item_id: str | None) -> str:
    """Build a tool_call_id that's stable across re-projections.

    Flowly's session storage matches assistant tool_calls to tool
    results by id. Using a deterministic id (item_type + item_id)
    means a re-stream of the same Codex turn produces identical
    tool_call_ids — useful for replay tests and idempotent message
    appending. Falls back to a random uuid only when Codex didn't
    supply an item_id.
    """
    if item_id:
        return f"codex_{item_type}_{item_id}"
    return f"codex_{item_type}_{uuid.uuid4().hex[:12]}"


def _truncate_for_message(text: str, max_chars: int = 8000) -> str:
    """Truncate long tool outputs so a giant ``cat huge_file`` doesn't
    blow up the session message store.

    Mirrors Flowly's existing tool-result truncation policy
    (``loop.py:_TOOL_MAX_CHARS``) so messages produced by the Codex
    path are the same size as messages produced by native tools.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... truncated from {len(text)} chars]"


# ---------------------------------------------------------------------------
# Projector
# ---------------------------------------------------------------------------


class CodexEventProjector:
    """Stateful sink for one Codex turn's item-stream events.

    Lifecycle: instantiate per-turn; feed every notification into
    :meth:`handle_notification`; call :meth:`finalize_turn` once
    ``turn/completed`` is observed. The instance is NOT reusable
    across turns — each turn gets a fresh projector so stale
    open-item buffers can't leak.

    Threading: single-asyncio-task. The stream callback is awaited
    inline as deltas arrive so the desktop renderer sees text the
    moment Codex emits it.
    """

    def __init__(
        self,
        *,
        stream_callback: StreamCallback | None = None,
    ) -> None:
        self._stream_callback = stream_callback

        # Items currently in progress, keyed by item_id. Filled by
        # ``item/started`` events, drained by ``item/completed``.
        self._open: dict[str, _OpenItem] = {}

        # Output buffers.
        self._messages: list[dict[str, Any]] = []
        self._reasoning_items: list[dict[str, Any]] = []
        self._tool_iterations: int = 0
        # Text of the LAST assistant message in the turn — used as
        # the ``final_text`` return value for the wrapping
        # ``codex_session`` tool. We carry this separately from the
        # messages list because a turn can end with a tool_call
        # assistant message followed by a tool result, and the
        # parent agent wants the *last text* the agent said, not the
        # last message's content (which might be a tool result).
        self._last_assistant_text: str = ""

        # ID of the most recent item whose text we streamed to the UI.
        # Used to detect when a new agentMessage starts after a
        # different item (commentary A → tool call → commentary B)
        # so we can insert a paragraph break in the stream. Without
        # this, Codex's separate agentMessage items render as one
        # un-broken blob — the chat UI sees consecutive deltas with
        # no separator between distinct messages.
        self._last_streamed_item_id: str | None = None

    # ── Public API ───────────────────────────────────────────────────

    async def handle_notification(self, note: dict[str, Any]) -> None:
        """Route one stdout notification to the right handler.

        Unknown / unhandled methods are logged at debug and skipped.
        Returns nothing; side effects on internal buffers + the
        stream callback only.
        """
        method = note.get("method", "")
        params = note.get("params") or {}

        if method == "item/started":
            self._on_item_started(params)
            return

        if method == "item/completed":
            self._on_item_completed(params)
            return

        if method.startswith("item/") and method.endswith("/delta"):
            # Method shape: "item/<type>/delta" — strip the prefix
            # and suffix to get the item-type slug.
            item_type = method[len("item/"):-len("/delta")]
            await self._on_item_delta(item_type, params)
            return

        if method == "turn/started":
            # Informational — session layer may use this to start
            # its watchdog timer, but the projector has nothing to
            # do with it.
            return

        if method == "turn/completed":
            # Informational here — the session calls finalize_turn()
            # explicitly. We just note that the turn is done so any
            # straggler deltas after this point get logged.
            return

        if method == "thread/started":
            # The first thread/start response carries the threadId
            # in the response result — the notification is a
            # courtesy that session.py can ignore.
            return

        # Anything else: unknown method, possibly a Codex schema
        # addition we don't know about yet. Log at debug (warn would
        # spam in production) and move on.
        logger.debug("[codex.projector] ignoring notification: %s", method)

    def finalize_turn(self) -> TurnProjection:
        """Flush any open items and return the projection result.

        Called by the session layer once ``turn/completed`` arrives.
        If any open items are still in progress (Codex dropped a
        ``completed`` event, or the turn was interrupted), they
        finalize with whatever buffer state they have — better a
        partial message than silently dropping content.
        """
        # Finalize any open items that didn't get a `completed` event.
        # This shouldn't normally happen but defensive — a dropped
        # event would otherwise lose all the deltas we accumulated.
        for item_id in list(self._open.keys()):
            self._on_item_completed({"itemId": item_id, "_forced": True})

        return TurnProjection(
            messages=list(self._messages),
            final_text=self._last_assistant_text,
            reasoning_items=list(self._reasoning_items),
            tool_iterations=self._tool_iterations,
        )

    # ── Item lifecycle handlers ──────────────────────────────────────

    @staticmethod
    def _unwrap_item(params: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical ``item`` payload, regardless of envelope shape.

        Codex 0.125 wraps the actual item state in a nested ``item``
        object: ``{"item": {"type": ..., "id": ..., <type fields>},
        "threadId": ..., "turnId": ...}``. Older builds/docs sometimes
        flattened it (``{"itemId": ..., "type": ..., ...}``). We accept
        either: prefer the nested ``item`` when present, fall back to
        the flat ``params`` otherwise.
        """
        nested = params.get("item")
        if isinstance(nested, dict):
            return nested
        return params

    def _on_item_started(self, params: dict[str, Any]) -> None:
        item = self._unwrap_item(params)
        item_id = item.get("id") or item.get("itemId") or params.get("itemId") or ""
        item_type = item.get("type") or params.get("type") or "unknown"
        if not item_id:
            # Without an item_id we can't correlate deltas → drop.
            # This would be a Codex protocol bug.
            logger.warning(
                "[codex.projector] item/started without item id: %r", params,
            )
            return
        if item_id in self._open:
            # Duplicate start — keep the existing buffer, log.
            logger.debug(
                "[codex.projector] item/started for already-open id=%s",
                item_id,
            )
            return
        self._open[item_id] = _OpenItem(
            item_id=item_id,
            item_type=item_type,
            # Capture the full item dict — Codex 0.125 ships most
            # final-state fields in item/started already (file diffs,
            # commands, etc.); item/completed mostly just flips status.
            payload=dict(item),
        )

    async def _on_item_delta(
        self, item_type: str, params: dict[str, Any],
    ) -> None:
        """Append one delta chunk to the open item.

        Codex 0.125 delta envelope: ``{"item": {"id": "...", <delta
        fields>}, "threadId": ..., "turnId": ...}``. The per-type
        delta-field names vary; this handler reads the common ones
        (``text``, ``delta``, ``outputDelta``, ``aggregatedOutput``,
        ``content``) from either the nested item or a flat params
        fallback.

        We capture text-bearing deltas, stream them to the callback
        for live UI updates, and buffer everything else as a generic
        payload merge.
        """
        body = self._unwrap_item(params)
        item_id = (
            body.get("id")
            or body.get("itemId")
            or params.get("itemId")
            or params.get("id")
            or ""
        )
        item = self._open.get(item_id)
        if item is None:
            # Delta for an item we never saw started — log + ignore.
            # Could happen if Codex restarts mid-turn or replays.
            logger.debug(
                "[codex.projector] delta for unknown item id=%s type=%s",
                item_id, item_type,
            )
            return

        # Text-bearing deltas. Codex emits agentMessage/userMessage
        # deltas as ``{text: "..."}`` or ``{delta: "..."}``.
        text = body.get("text") or body.get("delta")
        if isinstance(text, str) and text:
            item.text_parts.append(text)
            if item_type in ("agentMessage", "userMessage"):
                # User-facing text → stream it to the desktop renderer.
                if self._stream_callback is not None:
                    # Insert a paragraph break when switching to a
                    # different item (Codex emits one agentMessage
                    # per "thought" — without a separator between
                    # them, e.g. commentary A → tool call →
                    # commentary B, the chat shows one continuous
                    # blob: "...test entry point.The initial sandboxed
                    # read failed..."). On the very first stream call
                    # there's nothing to separate from, so we skip
                    # the prefix.
                    payload = text
                    if (
                        self._last_streamed_item_id is not None
                        and self._last_streamed_item_id != item.item_id
                    ):
                        payload = "\n\n" + text
                    try:
                        await self._stream_callback(payload)
                    except Exception:
                        # Stream callback errors are non-fatal —
                        # message still goes into the session, the
                        # user just doesn't see the live delta.
                        logger.debug(
                            "[codex.projector] stream_callback raised",
                            exc_info=True,
                        )
                    self._last_streamed_item_id = item.item_id

        # Tool-output-bearing items (exec stdout, mcp partial results):
        # accumulate separately so the final tool_result message has
        # the full text. Codex 0.125 uses ``aggregatedOutput`` for the
        # running exec stdout; older builds used ``outputDelta``.
        output = (
            body.get("aggregatedOutput")
            or body.get("outputDelta")
            or body.get("output")
        )
        if isinstance(output, str) and output:
            item.tool_output_parts.append(output)

        # Anything else: shallow-merge into payload so the completed
        # handler can read final state (diff content, exit code, etc.).
        merge_skip = {"id", "itemId", "type", "text", "delta",
                      "aggregatedOutput", "outputDelta", "output"}
        for k, v in body.items():
            if k in merge_skip:
                continue
            item.payload[k] = v

    def _on_item_completed(self, params: dict[str, Any]) -> None:
        """Finalize one open item into Flowly messages.

        Codex 0.125 wraps the final state under ``params["item"]``;
        we merge it over whatever we accumulated from started + deltas
        so the completed payload wins on conflicts (final text, final
        status, exit code, aggregated stdout, etc.).
        """
        body = self._unwrap_item(params)
        item_id = (
            body.get("id")
            or body.get("itemId")
            or params.get("itemId")
            or params.get("id")
            or ""
        )
        item = self._open.pop(item_id, None)
        if item is None:
            # Sometimes Codex emits item/completed without a matching
            # item/started in our buffer (e.g. when started arrived
            # carrying the full state we already projected). Build a
            # synthetic _OpenItem from the completed payload so the
            # handler still fires.
            if not item_id:
                logger.debug(
                    "[codex.projector] item/completed without id: %r",
                    params,
                )
                return
            item_type = body.get("type") or "unknown"
            item = _OpenItem(
                item_id=item_id,
                item_type=item_type,
                payload=dict(body),
            )

        merge_skip = {"id", "itemId", "type", "_forced"}
        for k, v in body.items():
            if k in merge_skip:
                continue
            item.payload[k] = v

        handler = self._handlers().get(item.item_type)
        if handler is None:
            logger.debug(
                "[codex.projector] no handler for item type=%s; skipping",
                item.item_type,
            )
            return
        handler(item)

    # ── Per-type handlers ────────────────────────────────────────────
    #
    # Each handler takes a finalized _OpenItem and appends to the
    # projection state. They run synchronously (no awaits) because
    # streaming has already happened during _on_item_delta — by the
    # time we get here we're just translating shapes.

    def _handlers(self) -> dict[str, Callable[[_OpenItem], None]]:
        # Built lazily so subclasses can override individual handlers
        # without re-wiring the dispatch table.
        return {
            "agentMessage": self._project_agent_message,
            "userMessage": self._project_user_message,
            "reasoning": self._project_reasoning,
            "commandExecution": self._project_command_execution,
            "fileChange": self._project_file_change,
            "mcpToolCall": self._project_mcp_tool_call,
            "dynamicToolCall": self._project_dynamic_tool_call,
            "webSearchCall": self._project_web_search_call,
        }

    def _project_agent_message(self, item: _OpenItem) -> None:
        """``agentMessage`` → ``{role: "assistant", content}``.

        Codex 0.125 marks each agentMessage with a ``phase``:
          * ``commentary`` — intermediate "thinking out loud" message
          * ``final_answer`` — the model's conclusion for the turn

        Both are projected as assistant messages so the chat surface
        shows the running commentary, but only ``final_answer`` text
        becomes ``_last_assistant_text`` — the value the wrapping
        ``codex_session`` tool returns as Codex's answer. Without that
        distinction, the parent agent would echo commentary fragments
        and miss the actual final conclusion.
        """
        text = item.payload.get("text") or "".join(item.text_parts)
        if not text:
            return
        self._messages.append({
            "role": "assistant",
            "content": text,
        })
        phase = item.payload.get("phase")
        if phase == "final_answer" or not phase:
            self._last_assistant_text = text

    def _project_user_message(self, item: _OpenItem) -> None:
        """``userMessage`` → ``{role: "user", content}``.

        Codex 0.125 ships userMessage content as a list of typed
        elements: ``content: [{type: "text", text: "..."}]``. Older
        shapes flatten to ``text``; we accept both.

        Rarely observed mid-turn — usually Codex echoes the user
        input back at the start of a turn so the projection is
        complete. We carry it through but note that the session
        layer already has the user message from the original input.
        """
        text = item.payload.get("text") or "".join(item.text_parts)
        if not text:
            content = item.payload.get("content") or []
            if isinstance(content, list):
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                text = "".join(parts)
        if not text:
            return
        self._messages.append({
            "role": "user",
            "content": text,
        })

    def _project_reasoning(self, item: _OpenItem) -> None:
        """``reasoning`` → stashed for continuity, NOT in messages.

        Codex's reasoning items contain encrypted_content blobs the
        model uses to continue its thinking on later turns. The blob
        location has moved between versions:

          * Flat (older): ``payload["encryptedContent"]``
          * Nested (0.125+): ``payload["content"]`` is a list with
            entries like ``{type: "reasoning_text"|"encrypted_reasoning",
            data|text: "..."}``

        We harvest from any of these. Items without any encrypted
        material are skipped — there's no value in replaying just a
        plaintext summary.
        """
        encrypted = (
            item.payload.get("encryptedContent")
            or item.payload.get("encrypted_content")
        )
        if not encrypted:
            # Look inside content[] for an encrypted_reasoning entry.
            content = item.payload.get("content") or []
            if isinstance(content, list):
                for entry in content:
                    if not isinstance(entry, dict):
                        continue
                    etype = entry.get("type", "")
                    if "encrypted" not in etype:
                        continue
                    blob = entry.get("data") or entry.get("text") or entry.get("content")
                    if isinstance(blob, str) and blob:
                        encrypted = blob
                        break
        if not encrypted:
            return
        self._reasoning_items.append({
            "itemId": item.item_id,
            "encryptedContent": encrypted,
            "summary": item.payload.get("summary"),
        })

    def _project_command_execution(self, item: _OpenItem) -> None:
        """``commandExecution`` → assistant tool_call + tool result.

        Codex 0.125 wire fields on the item:
          * ``command``: shell line (e.g. ``/bin/zsh -lc pwd``)
          * ``cwd``: working directory
          * ``aggregatedOutput``: full captured stdout+stderr (only
            populated when status is terminal — empty during
            ``inProgress``)
          * ``exitCode``: int, ``None`` while running
          * ``durationMs``: int wall-clock duration

        A streamed exec may also produce ``output``/``outputDelta``
        chunks we accumulate into ``tool_output_parts``; we prefer
        the streamed buffer when present, fall back to the final
        ``aggregatedOutput``.
        """
        command = item.payload.get("command", "")
        cwd = item.payload.get("cwd")
        output = (
            "".join(item.tool_output_parts)
            or item.payload.get("aggregatedOutput")
            or item.payload.get("output")
            or ""
        )
        exit_code = item.payload.get("exitCode")
        if exit_code is not None and exit_code != 0:
            output = (output or "").rstrip() + f"\n[exit_code={exit_code}]"

        self._emit_tool_pair(
            item=item,
            tool_name="exec",
            arguments={"command": command, **({"cwd": cwd} if cwd else {})},
            result=output or "(no output)",
        )
        self._tool_iterations += 1

    def _project_file_change(self, item: _OpenItem) -> None:
        """``fileChange`` → assistant tool_call (apply_patch) + tool result.

        Codex 0.125 ships fileChange items with a ``changes`` list,
        each carrying ``{path, kind: {type: "add"|"update"|"delete"},
        diff}``. A single item may touch multiple files (atomic
        multi-file patch); we surface them all together so the parent
        agent sees the whole change set in one tool result.
        """
        changes = item.payload.get("changes") or []
        if not isinstance(changes, list) or not changes:
            # Older flat shape fallback.
            path = item.payload.get("path") or item.payload.get("filePath", "")
            diff = item.payload.get("diff") or item.payload.get("unifiedDiff", "")
            changes = [{"path": path, "diff": diff, "kind": {"type": "update"}}]

        paths: list[str] = []
        diffs: list[str] = []
        summary_lines: list[str] = []
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            p = ch.get("path", "")
            kind_obj = ch.get("kind") or {}
            kind = (
                kind_obj.get("type")
                if isinstance(kind_obj, dict)
                else str(kind_obj)
            ) or "update"
            d = ch.get("diff") or ch.get("unifiedDiff", "")
            if p:
                paths.append(p)
            if d:
                diffs.append(d)
            summary_lines.append(f"{kind}: {p}")

        primary_path = paths[0] if paths else ""
        combined_diff = "\n".join(diffs)
        summary = "\n".join(summary_lines) if summary_lines else "(file changed)"

        self._emit_tool_pair(
            item=item,
            tool_name="apply_patch",
            arguments={"path": primary_path, "diff": combined_diff},
            result=summary,
        )
        self._tool_iterations += 1

    def _project_mcp_tool_call(self, item: _OpenItem) -> None:
        """``mcpToolCall`` → assistant tool_call with mcp.<server>.<tool> name.

        The MCP tool surface is dynamic — Codex routes each MCP tool
        call through the configured MCP server. We mirror the
        server.tool naming convention so the chat UI's tool icon
        labels stay informative.
        """
        server = item.payload.get("server", "unknown")
        tool = item.payload.get("tool", "unknown")
        args = item.payload.get("arguments") or {}
        result = (
            "".join(item.tool_output_parts)
            or item.payload.get("result")
            or item.payload.get("output", "")
        )
        if isinstance(result, (dict, list)):
            # MCP tool results are often structured JSON; serialise
            # for storage but keep them legible in the message store.
            result = json.dumps(result, ensure_ascii=False)[:8000]

        self._emit_tool_pair(
            item=item,
            tool_name=f"mcp.{server}.{tool}",
            arguments=args,
            result=str(result),
        )
        self._tool_iterations += 1

    def _project_dynamic_tool_call(self, item: _OpenItem) -> None:
        """``dynamicToolCall`` → assistant tool_call with the dynamic name.

        Codex's "dynamic" tools are agent-defined per-thread — we
        preserve the name verbatim.
        """
        name = item.payload.get("name", "dynamic")
        args = item.payload.get("arguments") or {}
        result = (
            "".join(item.tool_output_parts)
            or item.payload.get("result")
            or item.payload.get("output", "")
        )
        if isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False)[:8000]
        self._emit_tool_pair(
            item=item,
            tool_name=str(name),
            arguments=args,
            result=str(result),
        )
        self._tool_iterations += 1

    def _project_web_search_call(self, item: _OpenItem) -> None:
        """``webSearchCall`` → assistant tool_call (web_search) + result.

        Codex's built-in web search. Surfaced under the same name as
        Flowly's native web_search so the chat UI tool icon is
        consistent across providers.
        """
        query = item.payload.get("query", "")
        # The results payload is usually a list of {title, url, snippet}.
        results = item.payload.get("results") or []
        if isinstance(results, list):
            result_text = "\n\n".join(
                f"{r.get('title', '')}\n{r.get('url', '')}\n{r.get('snippet', '')[:300]}"
                for r in results if isinstance(r, dict)
            ) or "(no results)"
        else:
            result_text = str(results)

        self._emit_tool_pair(
            item=item,
            tool_name="web_search",
            arguments={"query": query},
            result=result_text,
        )
        self._tool_iterations += 1

    # ── Shared tool-pair emit ────────────────────────────────────────

    def _emit_tool_pair(
        self,
        *,
        item: _OpenItem,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
    ) -> None:
        """Append the assistant tool_call message + matching tool result.

        Two messages per tool call:
          1. ``{role: "assistant", content: "", tool_calls: [...]}``
          2. ``{role: "tool", tool_call_id, name, content}``

        The id is stable across re-projections (see
        :func:`_stable_tool_call_id`), so a re-stream of the same
        Codex turn produces byte-identical messages — useful for
        replay tests.
        """
        call_id = _stable_tool_call_id(item.item_type, item.item_id)
        # Truncate the result to Flowly's existing tool-output cap
        # so a giant `cat huge.log` doesn't bloat the session store.
        result_str = _truncate_for_message(str(result))

        # Try to serialise arguments to JSON for the tool_calls
        # ``function.arguments`` slot (matches OpenAI's tool schema
        # the rest of Flowly uses). On serialisation failure (rare,
        # but possible with non-JSON-serialisable types), fall back
        # to a stringified repr — the model can still read it.
        try:
            args_json = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            args_json = json.dumps({"_repr": repr(arguments)})

        self._messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": args_json,
                },
            }],
        })
        self._messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": result_str,
        })


__all__ = [
    "CodexEventProjector",
    "TurnProjection",
    "StreamCallback",
]
