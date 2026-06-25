"""Delegate tool — allows main agent to delegate tasks to other agents.

Runs agent subprocess in the background (fire-and-forget). The tool returns
immediately so the main agent can respond to the user. When the subprocess
completes, the result is automatically sent back to the user via the bus.
"""

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.config.schema import MultiAgentConfig, MultiAgentTeamConfig
from flowly.multiagent.invoke import invoke_agent, resolve_claude_model, resolve_codex_model


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
    ):
        self._agents = agents
        self._teams = teams
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

    async def execute(self, agent_id: str, message: str, **kwargs: Any) -> str:
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

        agent = self._agents[agent_id]
        model_display = self._resolve_model(agent)
        logger.info(f"Delegating to @{agent_id}: {message[:80]}...")

        # Capture context for the background task
        channel = self._channel
        chat_id = self._chat_id

        # Track running delegate + broadcast start event
        run_id = uuid.uuid4().hex[:8]
        self._running[run_id] = {
            'agent_id': agent_id,
            'label': f"@{agent_id}: {message[:60]}",
            'task': message[:200],
            'model': model_display,
            'started_at': time.time(),
        }
        if self._on_event:
            try:
                asyncio.ensure_future(self._on_event("subagent.started", {
                    "runId": run_id,
                    "label": f"@{agent_id}: {message[:60]}",
                    "task": message[:200],
                    "model": model_display,
                    "running": len(self._running),
                }))
            except Exception:
                pass

        async def _run_in_background() -> None:
            outcome = "error"  # default — overwritten on success
            content = ""
            try:
                result = await invoke_agent(
                    agent, agent_id, message, self._workspace, timeout=1800,
                )
                content = (
                    f"[DELEGATE_RESULT:{agent_id}] "
                    f"@{agent_id} has completed the task. "
                    f"Summarize the result for the user in your own words.\n\n"
                    f"Result:\n{result}"
                )
                outcome = "ok"
            except Exception as e:
                logger.error(f"Background delegation to @{agent_id} failed: {e}")
                content = (
                    f"[DELEGATE_RESULT:{agent_id}] "
                    f"@{agent_id} failed with an error. "
                    f"Tell the user what happened.\n\n"
                    f"Error: {e}"
                )
            finally:
                self._running.pop(run_id, None)
                if self._on_event:
                    try:
                        await self._on_event("subagent.completed", {
                            "runId": run_id,
                            "label": f"@{agent_id}",
                            "outcome": outcome,
                            "running": len(self._running),
                        })
                    except Exception:
                        pass

            # Send back through agent loop so the model summarizes it.
            # The DELEGATE_RESULT marker tells the routing layer to temporarily
            # remove the delegate_to tool, preventing re-delegation loops.
            if channel and chat_id:
                await self._bus.publish_inbound(
                    InboundMessage(
                        channel=channel,
                        sender_id="delegate_result",
                        chat_id=chat_id,
                        content=content,
                    )
                )
            else:
                logger.warning(f"No message context for @{agent_id} result delivery")

        # Fire-and-forget
        asyncio.create_task(_run_in_background())

        return (
            f"Task delegated to @{agent_id} ({agent.name or agent_id}, {model_display}). "
            f"The agent is now working in the background. "
            f"The result will be delivered automatically when the agent finishes."
        )
