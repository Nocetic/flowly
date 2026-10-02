"""Voice command execution through the runtime's ordinary ``exec`` tool.

The voice backend never receives its own shell. It asks this runtime to run
one command with the same registry entry, exec policy, approval manager,
hooks and cwd resolution as a normal chat turn. The voice conversation's
session key is the approval scope, so approval cards reach the same owner.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from flowly.compaction.redaction import redact_secrets
from flowly.live_voice.sessions import VoiceError, bounded_text, integer, session_key

if TYPE_CHECKING:
    from flowly.live_voice.sessions import VoiceSessions

MODEL_OUTPUT_CHARS = 24_000
STORED_OUTPUT_CHARS = 8_000
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 120
_EXIT_CODE = re.compile(r"\nExit code: (-?\d+)(?: \([^\n]*\))?\s*$")

# (tool params, session key) -> the tool's result string, exactly as a chat turn sees it.
Execute = Callable[[dict[str, Any], str], Awaitable[str]]


def exec_arguments(params: dict) -> dict:
    command = bounded_text(params.get("command"), "command", maximum=8000)
    arguments: dict[str, Any] = {"command": command}
    if params.get("workingDir") is not None:
        arguments["workingDir"] = bounded_text(params.get("workingDir"), "workingDir", maximum=1024)
    arguments["timeout"] = integer(params.get("timeout", DEFAULT_TIMEOUT_SECONDS), "timeout",
                                   minimum=1, maximum=MAX_TIMEOUT_SECONDS)
    return arguments


def bound(text: str, limit: int) -> tuple[str, bool]:
    """Keep the start and the end of long output; the tail usually holds the error."""
    if len(text) <= limit:
        return text, False
    marker = "\n… output truncated …\n"
    head = (limit - len(marker)) // 3
    return text[:head] + marker + text[-(limit - len(marker) - head):], True


def classify(result: str) -> tuple[str, int | None]:
    """Map the exec tool's documented result shapes to a record status."""
    text = result.lstrip()
    if text.startswith("❌ Command denied") or text.startswith("[blocked:"):
        return "denied", None
    if text.startswith("⏰ Command timed out"):
        return "timed_out", None
    if text.startswith("❌ Error") or text.startswith("Error"):
        return "failed", None
    match = _EXIT_CODE.search(result)
    return "completed", int(match.group(1)) if match else 0


class VoiceExec:
    """Runs voice commands for the primary runtime; one execution per command identity."""

    def __init__(self, execute: Execute, profile: Callable[[], str]):
        self._execute = execute
        self.profile = profile
        self._running: dict[tuple[str, str], asyncio.Task] = {}

    def running(self, conversation_id: str, command_id: str) -> bool:
        task = self._running.get((session_key(conversation_id), command_id))
        return task is not None and not task.done()

    async def run(self, sessions: VoiceSessions, conversation_id: str, record: dict, created: bool) -> dict:
        if record["status"] != "running":
            return receipt(record, replayed=True)
        key = (session_key(conversation_id), record["id"])
        task = self._running.get(key)
        if task is None:
            if not created:
                # A process restart lost the execution. Report it; never re-run.
                settled = sessions.finish_tool(conversation_id, record["id"], status="interrupted", output=None)
                return receipt(settled or {**record, "status": "interrupted"}, replayed=True)
            # The task inherits this request's owner scope, so approval events
            # and session checks stay bound to the conversation's owner.
            task = asyncio.create_task(self._run(sessions, conversation_id, record))
            self._running[key] = task
            task.add_done_callback(lambda _: self._running.pop(key, None))
        # A cancelled or disconnected request must not cancel the command.
        return await asyncio.shield(task)

    async def _run(self, sessions: VoiceSessions, conversation_id: str, record: dict) -> dict:
        arguments = record["arguments"]
        params: dict[str, Any] = {"command": arguments["command"], "timeout": arguments["timeout"]}
        if "workingDir" in arguments:
            params["working_dir"] = arguments["workingDir"]
        try:
            result = await self._execute(params, session_key(conversation_id))
            status, exit_code = classify(result)
        except Exception:
            result, status, exit_code = "Error: the command could not run on this agent.", "failed", None
        text = redact_secrets(result)
        model_output, model_truncated = bound(text, MODEL_OUTPUT_CHARS)
        stored, stored_truncated = bound(text, STORED_OUTPUT_CHARS)
        settled = sessions.finish_tool(conversation_id, record["id"], status=status, output=stored,
                                       truncated=stored_truncated, exit_code=exit_code)
        final = settled or {**record, "status": status, "output": stored, "truncated": stored_truncated,
                            "exitCode": exit_code}
        return {**receipt(final), "output": model_output, "truncated": model_truncated}


def receipt(record: dict, *, replayed: bool = False) -> dict:
    return {"status": record["status"], "output": record.get("output"), "truncated": bool(record.get("truncated")),
            "exitCode": record.get("exitCode"), "record": record, **({"replayed": True} if replayed else {})}
