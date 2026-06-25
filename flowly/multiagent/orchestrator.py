"""Team chain orchestrator — sequential and fan-out execution."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from flowly.config.schema import MultiAgentConfig, MultiAgentTeamConfig
from flowly.multiagent.invoke import invoke_agent
from flowly.multiagent.router import AgentRouter, TeamContext


MAX_CHAIN_DEPTH = 10


@dataclass
class ChainStep:
    """A single step in a team chain."""
    agent_id: str
    response: str


@dataclass
class OrchestratorResult:
    """Result of orchestrating an agent or team chain."""
    steps: list[ChainStep] = field(default_factory=list)

    @property
    def final_response(self) -> str:
        """Format the final response from all chain steps."""
        if len(self.steps) == 1:
            return self.steps[0].response
        return "\n\n---\n\n".join(
            f"@{step.agent_id}: {step.response}" for step in self.steps
        )


class TeamOrchestrator:
    """Executes agent invocations with team chain and fan-out support.

    Patterns:
    - Single agent: Direct invoke, return response.
    - Team chain: Leader invoked first. If response mentions a teammate,
      handoff continues. Chain ends when no teammate is mentioned or
      MAX_CHAIN_DEPTH is reached.
    - Fan-out: If response mentions multiple teammates (via tag format),
      all are invoked in parallel. Chain ends after fan-out.
    """

    def __init__(self, router: AgentRouter):
        self.router = router

    async def execute(
        self,
        message: str,
        agent_id: str,
        team_context: TeamContext | None,
        agents: dict[str, MultiAgentConfig],
        workspace: Path,
    ) -> OrchestratorResult:
        """Execute an agent invocation, optionally as a team chain.

        Args:
            message: User message (with @prefix stripped).
            agent_id: Target agent ID.
            team_context: Team context if routed via team, None for direct agent.
            agents: All configured agents.
            workspace: Base workspace path for agent directories.

        Returns:
            OrchestratorResult with all chain steps.
        """
        if not team_context:
            # Single agent — direct invoke
            response = await self._invoke_safe(agents, agent_id, message, workspace)
            return OrchestratorResult(steps=[ChainStep(agent_id, response)])

        # Team chain execution
        logger.info(f"Starting team chain: {team_context.team.name} (@{team_context.team_id})")
        steps: list[ChainStep] = []
        current_agent_id = agent_id
        current_message = message

        for depth in range(MAX_CHAIN_DEPTH):
            logger.info(f"Chain step {depth + 1}: invoking @{current_agent_id}")

            response = await self._invoke_safe(agents, current_agent_id, current_message, workspace)
            steps.append(ChainStep(current_agent_id, response))

            # Check for teammate mentions
            mentions = self.router.extract_teammate_mentions(
                response, current_agent_id, team_context.team_id
            )

            if not mentions:
                logger.info(f"Chain ended after {len(steps)} step(s) — no teammate mentioned")
                break

            if len(mentions) == 1:
                # Sequential handoff
                mention = mentions[0]
                logger.info(f"Chain handoff: @{current_agent_id} → @{mention.agent_id}")
                current_agent_id = mention.agent_id
                current_message = (
                    f"[Message from teammate @{steps[-1].agent_id}]:\n{mention.message}"
                )

            else:
                # Fan-out — parallel invoke
                logger.info(
                    f"Fan-out: @{current_agent_id} → "
                    f"{[m.agent_id for m in mentions]}"
                )
                fan_tasks = [
                    self._invoke_safe(
                        agents,
                        m.agent_id,
                        f"[Message from teammate @{current_agent_id}]:\n{m.message}",
                        workspace,
                    )
                    for m in mentions
                ]
                fan_results = await asyncio.gather(*fan_tasks, return_exceptions=True)

                for mention, result in zip(mentions, fan_results):
                    if isinstance(result, Exception):
                        steps.append(ChainStep(mention.agent_id, f"Error: {result}"))
                    else:
                        steps.append(ChainStep(mention.agent_id, result))

                logger.info(f"Fan-out complete — {len(fan_results)} responses collected")
                break  # Chain ends after fan-out

        else:
            logger.warning(f"Chain reached max depth ({MAX_CHAIN_DEPTH})")

        return OrchestratorResult(steps=steps)

    async def _invoke_safe(
        self,
        agents: dict[str, MultiAgentConfig],
        agent_id: str,
        message: str,
        workspace: Path,
    ) -> str:
        """Invoke an agent with error handling."""
        agent = agents.get(agent_id)
        if not agent:
            return f"Error: Agent '{agent_id}' not found."

        try:
            return await invoke_agent(agent, agent_id, message, workspace)
        except Exception as e:
            logger.error(f"Agent @{agent_id} invocation failed: {e}")
            return f"Error invoking @{agent_id}: {e}"
