"""Delegate tool — allows main agent to delegate tasks to other agents.

Runs agent subprocess in the background (fire-and-forget). The tool returns
immediately so the main agent can respond to the user. When the subprocess
completes, the result is automatically sent back to the user via the bus.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.agent.subagent_observation import TaskEvents, run_view
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
from flowly.agent.tools.base import Tool
from flowly.bus.events import InboundMessage
from flowly.bus.queue import MessageBus
from flowly.config.schema import MultiAgentConfig, MultiAgentTeamConfig
from flowly.multiagent.invoke import (
    invoke_agent,
    resolve_claude_model,
    resolve_codex_model,
)
from flowly.multiagent.orchestrator import TeamOrchestrator
from flowly.multiagent.router import AgentRouter, TeamContext


class DelegateTool(Tool):
    """Delegate a task to another configured agent.

    This tool allows the main Flowly agent to invoke specialized agents
    (Claude Code, Codex, etc.) for specific tasks. The agent subprocess
    runs in the background — the tool returns immediately with an
    acknowledgment, and the result is delivered asynchronously via the bus.
    """

    def __init__(
        self,
        agents: dict[str, MultiAgentConfig],
        teams: dict[str, MultiAgentTeamConfig],
        workspace: Path,
        bus: MessageBus,
        on_event: Any | None = None,
        registry: SubagentRegistry | None = None,
        max_concurrent: int = 5,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self._registry = registry or SubagentRegistry()
        self._events = TaskEvents(self._deliver_event)
        self._tasks: dict[str, asyncio.Task] = {}
        self._agents = agents
        self._teams = teams
        self._invocation_semaphore = asyncio.Semaphore(max_concurrent)
        self._orchestrator = TeamOrchestrator(
            AgentRouter(agents, teams), max_concurrent, self._invocation_semaphore
        )
        self._workspace = workspace
        self._bus = bus
        self._on_event = on_event  # async callback(event_name, data) for UI notifications
        self._running: dict[str, dict] = {}  # run_id → { agent_id, label, task, model, started_at }
        # Current message context — set before each execution by the routing layer
        self._channel: str = ""
        self._chat_id: str = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context for async result delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "delegate_to"

    def _resolve_model(self, cfg: MultiAgentConfig) -> str:
        """Resolve short model name to full model ID for display."""
        if cfg.provider == "anthropic":
            return resolve_claude_model(cfg.model)
        if cfg.provider == "openai":
            return resolve_codex_model(cfg.model)
        return cfg.model

    @property
    def description(self) -> str:
        agent_list = ", ".join(
            f"@{aid} ({cfg.name or aid}, {self._resolve_model(cfg)})"
            for aid, cfg in self._agents.items()
        )
        return (
            "Delegate a task to another specialized agent. "
            "IMPORTANT: Pass the user's message EXACTLY as-is in the 'message' parameter — "
            "do NOT rephrase, summarize, or add your own interpretation. "
            "The task runs in the background — tell the user it has been delegated. "
            f"Available agents: {agent_list}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        agent_ids = list(self._agents.keys())
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": f"Target agent ID. Available: {agent_ids}",
                },
                "message": {
                    "type": "string",
                    "description": "The task or message to send to the agent.",
                },
            },
            "required": ["agent_id", "message"],
        }

    async def execute(
        self, agent_id: str, message: str, *, team_id: str | None = None, **kwargs: Any
    ) -> str:
        """Delegate a task to the specified agent (fire-and-forget).

        Starts the agent subprocess in the background and returns immediately.
        When the subprocess completes, the result is sent via the bus.

        Args:
            agent_id: Target agent identifier.
            message: Task/message for the agent.

        Returns:
            Immediate acknowledgment string (result comes async via bus).
        """
        if agent_id not in self._agents:
            available = list(self._agents.keys())
            return f"Error: Agent '{agent_id}' not found. Available agents: {available}"

        team = self._teams.get(team_id) if team_id else None
        if team_id and (team is None or team.leader_agent != agent_id):
            return f"Error: Team '{team_id}' not found for leader @{agent_id}."

        agent = self._agents[agent_id]
        target_id = team_id or agent_id
        model_display = self._resolve_model(agent)
        logger.info(f"Delegating to @{target_id}: {message[:80]}...")

        # Capture context for the background task
        channel = self._channel
        chat_id = self._chat_id

        run_id = str(uuid.uuid4())
        record = SubagentRunRecord(
            run_id=run_id, child_session_key=f"delegate:{run_id}",
            parent_session_key=f"{channel}:{chat_id}", parent_channel=channel,
            parent_chat_id=chat_id, task=message, label=target_id,
            display_name=f"@{target_id}: {message[:100]}", model=model_display,
            cleanup="keep", created_at=time.time(), kind="delegate", agent_id=target_id,
            activity={"phase": "queued"},
            delivery_state="pending" if channel and chat_id else "not_required",
        )
        self._registry.register(record)
        self._running[run_id] = {"agent_id": target_id, "label": record.display_name,
                                 "task": message, "model": model_display, "started_at": record.created_at}
        self._publish_event("subagent.started", run_id)

        async def _run_in_background() -> None:
            result = ""
            try:
                self._registry.update(run_id, started_at=time.time(), activity={"phase": "working"})
                self._publish_event("subagent.progress", run_id)
                if team is not None:
                    team_result = await self._orchestrator.execute(
                        message, agent_id, TeamContext(team_id, team),
                        self._agents, self._workspace,
                    )
                    result = team_result.final_response
                else:
                    async with self._invocation_semaphore:
                        result = await invoke_agent(
                            agent, agent_id, message, self._workspace, timeout=1800
                        )
                if not result.strip():
                    raise ValueError("Task produced no output")
                self._registry.finish(run_id, "ok", result=result)
            except asyncio.CancelledError:
                self._registry.finish(run_id, "cancelled", error="Task interrupted", error_code="cancelled")
                raise
            except Exception as exc:
                logger.error(f"Background delegation to @{target_id} failed: {exc}")
                self._registry.finish(run_id, "error", error="Task could not finish", error_code="delegate_failed")
            finally:
                self._running.pop(run_id, None)
                self._publish_event("subagent.completed", run_id)

            if channel and chat_id:
                current = self._registry.get(run_id)
                outcome = "completed" if current.outcome == "ok" else "failed"
                content = (f"[DELEGATE_RESULT:{target_id}] @{target_id} {outcome}. "
                           f"Summarize the result for the user.\n\n{result or current.error}")
                await self._bus.publish_inbound(InboundMessage(
                    channel=channel, sender_id="delegate_result", chat_id=chat_id, content=content,
                ))
                self._registry.update(run_id, announced=True, delivery_state="queued")

        task = asyncio.create_task(_run_in_background())
        self._tasks[run_id] = task

        def cleanup(done: asyncio.Task) -> None:
            self._tasks.pop(run_id, None)
            self._running.pop(run_id, None)
            try:
                record = self._registry.get(run_id)
                if record and record.ended_at is None:
                    self._registry.finish(run_id, "cancelled" if done.cancelled() else "error",
                                          error="Task interrupted", error_code="worker_stopped")
                    self._publish_event("subagent.completed", run_id)
            except Exception:
                logger.exception("Could not persist delegated task cleanup")
            finally:
                if not done.cancelled():
                    done.exception()

        task.add_done_callback(cleanup)
        target_name = team.name if team is not None else agent.name or agent_id
        return (f"Task delegated to @{target_id} ({target_name}, {model_display}). "
                "The agent is now working in the background. "
                "The result will be delivered automatically when the agent finishes.")

    async def _deliver_event(self, event: str, data: dict) -> None:
        if self._on_event:
            await self._on_event(event, data)

    def _publish_event(self, event: str, run_id: str) -> None:
        try:
            record = self._registry.get(run_id)
            if record:
                data = run_view(record)
                data.update(outcome=record.outcome, running=len(self._registry.pending()))
                self._events.publish(event, data)
        except Exception:
            logger.exception("Delegated task observation unavailable")

    async def cancel(self, run_id: str) -> str:
        task = self._tasks.get(run_id)
        if task is None:
            return json.dumps({"status": "error", "error": "Task is no longer running"})
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return json.dumps({"status": "cancelled", "run_id": run_id})

    def cancel_all(self) -> int:
        active = [task for task in self._tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        return len(active)
