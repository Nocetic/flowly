"""The wire rows of a conversation's display history (``chat.history``).

One projection for every RPC that returns history rows — the direct
gateway's ``chat.history`` and ``subagents.history`` — so a client renders a
subagent's conversation with the same code as the owner's.
"""

from __future__ import annotations

from typing import Any, Callable

# Builds the inline attachments of a row's media (paths, descriptors) or None.
AttachmentBuilder = Callable[[list[Any], Any], list[dict[str, Any]] | None]


def history_row(m: dict[str, Any], *, attachments: AttachmentBuilder | None = None) -> dict[str, Any]:
    """One stored message as a history row.

    The tool-protocol fields (``tool_calls`` on assistant turns,
    ``tool_call_id``/``name`` on tool results) ride along so a client with no
    Firestore can rebuild the live tool-turn panel from history alone.
    """
    msg: dict[str, Any] = {"role": m["role"]}
    # Normalise content into array format expected by the iOS/desktop protocol.
    content_raw = m.get("content", "")
    if isinstance(content_raw, str):
        msg["content"] = [{"type": "text", "text": content_raw}]
    elif isinstance(content_raw, list):
        msg["content"] = content_raw
    else:
        msg["content"] = [{"type": "text", "text": str(content_raw)}]
    if m.get("tool_calls"):
        msg["tool_calls"] = m["tool_calls"]
    if m.get("tool_call_id"):
        msg["tool_call_id"] = m["tool_call_id"]
    if m.get("name"):
        msg["name"] = m["name"]
    # Context-boundary rows (a compaction happened here). Typed fields ride
    # alongside the legacy text so a client can render the divider without
    # matching a string — the relay's Firestore row carries the same pair, so
    # both transports look identical to a client.
    if m.get("kind"):
        msg["kind"] = m["kind"]
    if m.get("kind") == "voice" and isinstance(m.get("voice"), dict):
        msg["voice"] = m["voice"]
    if m.get("kind") == "agent_setup_card" and isinstance(m.get("setupCard"), dict):
        msg["setupCard"] = m["setupCard"]
    if isinstance(m.get("goal_cards"), list) and m["goal_cards"]:
        msg["goalCards"] = m["goal_cards"]
    if m.get("boundaryKind"):
        msg["boundaryKind"] = m["boundaryKind"]
    if m.get("compactionId"):
        msg["compactionId"] = m["compactionId"]
    # Media on this turn — user-attached inbound files and media the agent
    # produced — in the inline-thumbnail shape, so a remote client renders the
    # preview from history with no fetch, matching the live reply path.
    media_paths = m.get("media")
    if attachments is not None and isinstance(media_paths, list) and media_paths:
        atts = attachments(media_paths, m.get("media_assets"))
        if atts:
            msg["attachments"] = atts
    if "timestamp" in m:
        msg["timestamp"] = m["timestamp"]
    if "usage" in m:
        msg["usage"] = m["usage"]
    if m.get("aborted") is True:
        msg["aborted"] = True
    if m.get("id"):
        msg["id"] = m["id"]
    if m.get("steering_run_id"):
        msg["steeringRunId"] = m["steering_run_id"]
    for source, target in (
        ("steering_sequence", "steeringSequence"),
        ("steering_source_run_id", "steeringSourceRunId"),
        ("steering_message_id", "steeringMessageId"),
        ("steering_iteration_idx", "steeringIterationIdx"),
        ("steering_previous_iteration_idx", "steeringPreviousIterationIdx"),
        ("steering_tool_call_ids", "steeringToolCallIds"),
    ):
        if source in m:
            msg[target] = m[source]
    run_id = m.get("run_id")
    if isinstance(run_id, str) and run_id:
        msg["runId"] = run_id
    duration_ms = m.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        msg["durationMs"] = max(0, int(duration_ms))
    return msg


def subagent_history_row(m: dict[str, Any]) -> dict[str, Any]:
    """A subagent conversation's row: :func:`history_row` plus what a subagent turn says.

    ``kind`` is ``subagent_task`` for the task the main agent gave (``author:
    "main_agent"``, ``turn``, ``trigger``; the bare ``brief`` when the row
    says more than the task — the first turn's forked conversation
    (``withConversation``), a later turn's label and the messages that waited
    for it) and ``agent_note`` for a message from the main agent read mid-run
    (``noteKind: "task"`` for a task it gave the running turn). A tool row
    carries ``status`` (``ok``, ``error``, ``timeout``, ``interrupted``,
    ``not_run``); the closing row of a turn its ``outcome``.
    """
    msg = history_row(m)
    if isinstance(m.get("author"), str):
        msg["author"] = m["author"]
    if type(m.get("turn")) is int:
        msg["turn"] = m["turn"]
    if m.get("trigger") in ("spawn", "followup"):
        msg["trigger"] = m["trigger"]
    if m.get("kind") == "subagent_task":
        if m.get("with_conversation") is True:
            msg["withConversation"] = True
        if isinstance(m.get("brief"), str):
            msg["brief"] = m["brief"]
    if m.get("kind") == "agent_note" and m.get("noteKind") == "task":
        msg["noteKind"] = "task"
    if m.get("role") == "tool" and isinstance(m.get("status"), str):
        msg["status"] = m["status"]
    if isinstance(m.get("outcome"), str):
        msg["outcome"] = m["outcome"]
    return msg


def project_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten deferred-call wrappers so history shows the tool identity the live view did."""
    from flowly.tool_activity import project_tool_messages_for_ui

    return project_tool_messages_for_ui(rows)
