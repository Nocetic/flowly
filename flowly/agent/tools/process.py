"""Background process tool — long-running subprocess management.

Wraps ``flowly.exec.process_registry.ProcessRegistry`` for the agent. Use
when a command would otherwise block a foreground ``exec`` call: dev
servers, ML experiments, watchers. The tool returns immediately with a
session_id; the agent polls or registers a watch_pattern to learn when
something interesting happens.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.exec.process_registry import ProcessRegistry


class ProcessTool(Tool):
    """Multi-action tool for background process management.

    Actions:
      - start: spawn command, return session_id immediately
      - list: enumerate running + recently-finished sessions
      - poll: check status + last output preview (non-blocking)
      - log: read full buffered output (with line pagination)
      - wait: block until exit or timeout
      - kill: terminate a running session

    ``session_key`` is auto-injected by the agent loop before each call so
    completion events route back to the right channel/chat.
    """

    def __init__(self, registry: ProcessRegistry):
        self._registry = registry

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return """Manage long-running background processes (dev servers, experiments, watchers).

Use this tool — NOT 'exec' — when a command will run for more than ~30 seconds, when you need to
keep something running while you do other work, or when you want to be notified when a specific
string appears in the output.

Actions:
  - start: spawn a command in the background. Returns session_id immediately.
      params: command (required), watch_patterns (optional list of substrings),
              notify_on_complete (default true), cwd (optional)
      The tool returns control to you instantly — DO NOT wait. Continue with other work and
      poll/wait later. If watch_patterns or notify_on_complete is set you will receive a
      synthetic system message when the pattern matches or process exits.
  - list: show running + recently-finished sessions for this conversation.
  - poll: check status + last 1000 chars of output (non-blocking).
      params: session_id (required)
  - log: read fuller output with line pagination.
      params: session_id (required), offset (default 0, negative counts from end), limit
  - wait: block until the process exits or timeout.
      params: session_id (required), timeout (seconds, optional)
  - kill: terminate a running session (SIGTERM, then SIGKILL).
      params: session_id (required)

Background processes survive across tool calls; their output is captured in a rolling 200KB
buffer. Up to 64 concurrent processes; oldest finished are evicted first."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "list", "poll", "log", "wait", "kill"],
                    "description": "The action to perform.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to spawn (required for action=start).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Process session id (required for poll/log/wait/kill).",
                },
                "watch_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Substrings to watch for in stdout. When matched, you receive a "
                        "synthetic system message. Rate-limited to one match per 15s per "
                        "session; auto-disables after 3 strikes."
                    ),
                },
                "notify_on_complete": {
                    "type": "boolean",
                    "description": (
                        "Send a synthetic system message when the process exits "
                        "(default true)."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for action=start (default: agent's cwd).",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Starting line index for action=log (default 0; negative counts "
                        "from the end of the buffer)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of lines for action=log.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait for action=wait (default: no timeout).",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, session_key: str | None = None, **kwargs: Any) -> str:
        try:
            if action == "start":
                return await self._action_start(session_key=session_key, **kwargs)
            if action == "list":
                return await self._action_list(session_key=session_key)
            if action == "poll":
                return await self._action_poll(**kwargs)
            if action == "log":
                return await self._action_log(**kwargs)
            if action == "wait":
                return await self._action_wait(**kwargs)
            if action == "kill":
                return await self._action_kill(**kwargs)
            return f"Unknown action: {action!r} (expected one of: start, list, poll, log, wait, kill)"
        except Exception as e:
            logger.error(f"[ProcessTool] {action} failed: {e}")
            return f"Error in process({action!r}): {e}"

    async def _action_start(
        self,
        command: str | None = None,
        watch_patterns: list[str] | None = None,
        notify_on_complete: bool = True,
        cwd: str | None = None,
        session_key: str | None = None,
        **_: Any,
    ) -> str:
        if not command or not isinstance(command, str) or not command.strip():
            return "Error: 'command' is required for action=start"
        try:
            session = await self._registry.spawn(
                command=command,
                session_key=session_key,
                cwd=cwd,
                watch_patterns=watch_patterns,
                notify_on_complete=bool(notify_on_complete),
            )
        except RuntimeError as e:
            return f"Error: {e}"
        return json.dumps({
            "status": "started",
            "session_id": session.id,
            "pid": session.pid,
            "command": session.command,
            "watch_patterns": session.watch_patterns,
            "notify_on_complete": session.notify_on_complete,
            "next_steps": (
                "Process is running in the background. DO NOT wait for it — continue with other "
                "work. You will be notified via a system message when it exits or when a "
                "watch_pattern matches. To check progress sooner, call process(action='poll', "
                f"session_id='{session.id}')."
            ),
        })

    async def _action_list(self, session_key: str | None = None) -> str:
        sessions = await self._registry.list_sessions(session_key=session_key)
        return json.dumps({"sessions": sessions, "count": len(sessions)})

    async def _action_poll(self, session_id: str | None = None, **_: Any) -> str:
        if not session_id:
            return "Error: 'session_id' is required for action=poll"
        return json.dumps(await self._registry.poll(session_id))

    async def _action_log(
        self,
        session_id: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        **_: Any,
    ) -> str:
        if not session_id:
            return "Error: 'session_id' is required for action=log"
        return json.dumps(await self._registry.read_log(session_id, offset=offset, limit=limit))

    async def _action_wait(
        self,
        session_id: str | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> str:
        if not session_id:
            return "Error: 'session_id' is required for action=wait"
        return json.dumps(await self._registry.wait(session_id, timeout=timeout))

    async def _action_kill(self, session_id: str | None = None, **_: Any) -> str:
        if not session_id:
            return "Error: 'session_id' is required for action=kill"
        return json.dumps(await self._registry.kill(session_id))
