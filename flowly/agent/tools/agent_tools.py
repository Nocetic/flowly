"""Tools the agent uses to work with the subagents it started."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from flowly.agent.tool_context import current_tool_origin
from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.subagent import SubagentManager
    from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord

_READ_DEFAULT_CHARS = 8_000
_READ_MAX_CHARS = 20_000
_MIN_ID_PREFIX = 8


def _status(record: "SubagentRunRecord") -> str:
    return "running" if record.ended_at is None else (record.outcome or "unknown")


def resolve_own_run(
    registry: "SubagentRegistry", run_id: str, session_key: str | None,
) -> tuple["SubagentRunRecord | None", str]:
    """Find a run this conversation started, by full id or unique prefix.

    Another conversation's run reads as "not found": its existence is not
    this conversation's business.
    """
    wanted = (run_id or "").strip()
    if session_key is None:
        # No conversation is running this call (a scheduled tool job, an
        # external caller): it owns no subagents.
        return None, "No conversation owns this call, so it has no subagents."
    if len(wanted) < _MIN_ID_PREFIX:
        return None, "Give the run_id from the subagent's start or completion message."
    candidates = [
        record for record in registry.all()
        if record.run_id == wanted or record.run_id.startswith(wanted)
    ]
    candidates = [r for r in candidates if r.parent_session_key == session_key]
    if not candidates:
        return None, f"No subagent run {wanted} in this conversation."
    if len(candidates) > 1:
        return None, f"run_id {wanted} matches several runs; give the full id."
    return candidates[0], ""


class AgentReadTool(Tool):
    """Read a subagent's status and full result."""

    def __init__(self, registry: "SubagentRegistry"):
        self._registry = registry

    @property
    def name(self) -> str:
        return "agent_read"

    @property
    def description(self) -> str:
        return (
            "Read the rest of a finished subagent's result, page by page — when "
            "its completion message says more text is available, or to get back "
            "a result from an earlier turn you no longer have. A result that "
            "already reached you is not new: use it instead of reading it again. "
            "Don't use this to check on running subagents; their results reach "
            "you as a message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The subagent's run_id."},
                "offset": {
                    "type": "integer",
                    "description": "Character offset to read from (default 0).",
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Characters to read (default {_READ_DEFAULT_CHARS}).",
                    "minimum": 1,
                    "maximum": _READ_MAX_CHARS,
                },
            },
            "required": ["run_id"],
        }

    async def execute(
        self, run_id: str, offset: int = 0, limit: int = _READ_DEFAULT_CHARS, **kwargs: Any,
    ) -> str:
        origin = current_tool_origin()
        record, problem = resolve_own_run(
            self._registry, run_id, origin.session_key if origin else None,
        )
        if record is None:
            return json.dumps({"error": problem})
        try:
            offset = max(0, int(offset))
            limit = max(1, min(int(limit), _READ_MAX_CHARS))
        except (TypeError, ValueError):
            return json.dumps({"error": "offset and limit must be whole numbers."})

        view: dict[str, Any] = {
            "run_id": record.run_id,
            "title": record.display_name or record.label,
            "status": _status(record),
            "model": record.model,
        }
        if record.error:
            view["error"] = record.error
        if record.result_available:
            try:
                page = self._registry.read_result(record.run_id, offset, limit)
            except (FileNotFoundError, ValueError) as exc:
                view["result_error"] = str(exc)
            else:
                view.update(
                    content=page["content"],
                    offset=page["offset"],
                    next_offset=page["nextOffset"],
                    total_chars=page["totalChars"],
                )
        elif record.ended_at is None:
            view["note"] = "Still running; its result will reach you as a message."
        return json.dumps(view, ensure_ascii=False)


_REPLY_WAIT_SECONDS = 300.0  # keep in step with subagent._REPLY_WAIT_SECONDS


class AgentMessageTool(Tool):
    """The main agent's line to the subagents it started, while they run."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "agent_message"

    @property
    def description(self) -> str:
        return (
            "Send a message to one or more of your running subagents — to steer, "
            "narrow or answer them. Each reads it at its next step; it does not "
            "interrupt a tool call in progress. Use to=\"all\" for every subagent "
            "of this conversation that is still running."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "description": "A run_id, a list of run_ids, or \"all\".",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "message": {"type": "string", "description": "What to tell them."},
            },
            "required": ["to", "message"],
        }

    async def execute(self, to: Any, message: str, **kwargs: Any) -> str:
        text = (message or "").strip()
        if not text:
            return json.dumps({"error": "The message is empty."})
        origin = current_tool_origin()
        session_key = origin.session_key if origin else None
        if session_key is None:
            return json.dumps({"error": "No conversation owns this call, so it has no subagents."})
        registry = self._manager.registry
        if to == "all":
            targets = [
                record.run_id for record in registry.all()
                if record.ended_at is None and record.origin != "system"
                and record.parent_session_key == session_key
            ]
            if not targets:
                return json.dumps({"error": "No subagent of this conversation is running."})
        else:
            wanted = [to] if isinstance(to, str) else list(to or [])
            targets = []
            for item in wanted:
                record, problem = resolve_own_run(registry, str(item), session_key)
                if record is None:
                    return json.dumps({"error": problem})
                targets.append(record.run_id)
        results = [await self._manager.send_to_child(run_id, text) for run_id in dict.fromkeys(targets)]
        return json.dumps({"sent": results}, ensure_ascii=False)


class ChildAgentMessageTool(Tool):
    """A subagent's line to the main agent that started it."""

    def __init__(self, manager: "SubagentManager", run_id: str):
        self._manager = manager
        self._run_id = run_id

    @property
    def name(self) -> str:
        return "agent_message"

    @property
    def description(self) -> str:
        return (
            "Send a message to the main agent that started you — a question you "
            "can't resolve yourself, or something it must know before you finish. "
            "Set await_reply to wait (up to 5 minutes) for its answer; otherwise "
            "keep working and its reply reaches you at a later step."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to tell or ask."},
                "await_reply": {
                    "type": "boolean",
                    "description": "Wait for the main agent's answer before going on.",
                },
            },
            "required": ["message"],
        }

    async def execute(self, message: str, await_reply: bool = False, **kwargs: Any) -> str:
        text = (message or "").strip()
        if not text:
            return json.dumps({"error": "The message is empty."})
        sent = await self._manager.message_parent(self._run_id, text)
        if sent.get("status") != "delivered":
            return json.dumps({"error": "The main agent could not be reached."})
        if not await_reply:
            return json.dumps({"status": "sent", "to": "orchestrator"})
        replies = await self._manager._wait_for_inbox(self._run_id, _REPLY_WAIT_SECONDS)
        if not replies:
            return json.dumps({
                "status": "sent", "to": "orchestrator",
                "reply": None, "note": "No reply within 5 minutes; continue with your best judgment.",
            })
        return json.dumps({"status": "sent", "to": "orchestrator", "reply": "\n\n".join(replies)},
                          ensure_ascii=False)


_WAIT_DEFAULT_SECONDS = 30
_WAIT_MAX_SECONDS = 120
# All the waiting one turn may do. Past it the turn ends and the results
# arrive as a message, so the owner is never left behind a busy chat.
_WAIT_TURN_BUDGET_SECONDS = 300


class AgentWaitTool(Tool):
    """Wait for this conversation's subagents instead of polling them."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "agent_wait"

    @property
    def description(self) -> str:
        return (
            "Rarely needed. Results of subagents you started reach you by "
            "themselves, together, as a new message once they have all "
            "finished — so after starting them, end your turn. Wait only when "
            "you are in the middle of your own work in this turn and cannot "
            "finish it without a result expected within a minute or two. "
            "Returns as soon as one finishes or sends you a message, when the "
            "owner writes, or at the timeout. Never sleep or check task lists "
            "instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subagents to wait for (default: all of this conversation's).",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _WAIT_MAX_SECONDS,
                    "description": f"Longest wait (default {_WAIT_DEFAULT_SECONDS}).",
                },
            },
        }

    async def execute(
        self, run_ids: list[str] | None = None, timeout_seconds: int = _WAIT_DEFAULT_SECONDS,
        **kwargs: Any,
    ) -> str:
        from flowly.agent.delegation import current_turn

        origin = current_tool_origin()
        session_key = origin.session_key if origin else None
        if session_key is None:
            return json.dumps({"error": "No conversation owns this call, so it has no subagents."})
        resolved: list[str] | None = None
        if run_ids:
            resolved = []
            for item in run_ids:
                record, problem = resolve_own_run(self._manager.registry, str(item), session_key)
                if record is None:
                    return json.dumps({"error": problem})
                resolved.append(record.run_id)
        try:
            timeout = max(1, min(int(timeout_seconds), _WAIT_MAX_SECONDS))
        except (TypeError, ValueError):
            timeout = _WAIT_DEFAULT_SECONDS
        turn = current_turn()
        if turn is not None:
            left = _WAIT_TURN_BUDGET_SECONDS - turn.waited[0]
            if left < 1:
                return json.dumps({
                    "finished": [], "messages": [], "timed_out": True, "interrupted": False,
                    "still_running": [
                        r.display_name or r.label for r in self._manager.registry.all()
                        if r.parent_session_key == session_key and r.ended_at is None
                        and r.origin != "system"
                    ],
                    "note": "This turn has waited long enough. End your turn now: tell "
                            "the owner briefly what is still running. The results "
                            "reach you as a message when they finish.",
                }, ensure_ascii=False)
            timeout = min(timeout, int(left))
        started = time.monotonic()
        try:
            outcome = await self._manager.wait_for_children(
                session_key, resolved, timeout=timeout,
                should_yield=turn.should_yield if turn else None,
            )
        finally:
            if turn is not None:
                turn.waited[0] += time.monotonic() - started
        if outcome["interrupted"] and turn is not None and not turn.steerable:
            outcome["note"] = (
                "The owner has sent a new message; you'll answer it right after "
                "this. Finish this reply now with what you have."
            )
        elif outcome["interrupted"]:
            outcome["note"] = "The owner wrote to you; read it and answer before waiting again."
        elif outcome["timed_out"] and outcome["still_running"]:
            outcome["note"] = (
                "Still running. Unless your own work in this turn needs them, end "
                "your turn: tell the owner briefly what is running. The results "
                "reach you as a message when they finish."
            )
        elif not (outcome["finished"] or outcome["messages"] or outcome["still_running"]):
            outcome["note"] = "No subagent of this conversation is running or waiting to be read."
        return json.dumps(outcome, ensure_ascii=False)
