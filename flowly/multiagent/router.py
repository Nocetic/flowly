"""Agent routing — @mention parsing and message routing."""

import re
from dataclasses import dataclass, field

from flowly.config.schema import MultiAgentConfig, MultiAgentTeamConfig


@dataclass
class RoutingResult:
    """Result of routing a message to an agent."""
    agent_id: str
    message: str
    is_team: bool = False


@dataclass
class TeammateMention:
    """A teammate mention extracted from agent response."""
    agent_id: str
    message: str


@dataclass
class TeamContext:
    """Team context for chain execution."""
    team_id: str
    team: MultiAgentTeamConfig


class AgentRouter:
    """Routes messages to agents based on @mention prefix.

    Supports:
    - @agent_id → direct agent routing
    - @team_id → team leader routing
    - @agent_name → case-insensitive name match
    - No prefix → default agent
    """

    def __init__(
        self,
        agents: dict[str, MultiAgentConfig],
        teams: dict[str, MultiAgentTeamConfig],
    ):
        self.agents = agents
        self.teams = teams

    def route(self, message: str) -> RoutingResult:
        """Parse @agent_id or @team_id prefix from message.

        Args:
            message: Raw message text.

        Returns:
            RoutingResult with agent_id, cleaned message, and team flag.
        """
        match = re.match(r"^@(\S+)\s+([\s\S]*)", message)
        if not match:
            return RoutingResult(agent_id="default", message=message)

        candidate = match.group(1).lower()
        clean_message = match.group(2)

        # Direct agent ID match
        if candidate in self.agents:
            return RoutingResult(agent_id=candidate, message=clean_message)

        # Team ID match → leader agent
        if candidate in self.teams:
            team = self.teams[candidate]
            return RoutingResult(
                agent_id=team.leader_agent, message=clean_message, is_team=True
            )

        # Agent name match (case-insensitive)
        for agent_id, config in self.agents.items():
            if config.name.lower() == candidate:
                return RoutingResult(agent_id=agent_id, message=clean_message)

        # Team name match (case-insensitive)
        for team_id, team in self.teams.items():
            if team.name.lower() == candidate:
                return RoutingResult(
                    agent_id=team.leader_agent, message=clean_message, is_team=True
                )

        # No match — default
        return RoutingResult(agent_id="default", message=message)

    def find_team_for_agent(self, agent_id: str) -> TeamContext | None:
        """Find the first team that contains the given agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            TeamContext if agent belongs to a team, None otherwise.
        """
        for team_id, team in self.teams.items():
            if agent_id in team.agents:
                return TeamContext(team_id=team_id, team=team)
        return None

    def is_teammate(
        self, mentioned_id: str, current_agent_id: str, team_id: str
    ) -> bool:
        """Check if mentioned_id is a valid teammate of current_agent in the given team."""
        team = self.teams.get(team_id)
        if not team:
            return False
        return (
            mentioned_id != current_agent_id
            and mentioned_id in team.agents
            and mentioned_id in self.agents
        )

    def extract_teammate_mentions(
        self, response: str, current_agent_id: str, team_id: str
    ) -> list[TeammateMention]:
        """Extract @teammate mentions from agent response.

        Supports two formats:
        1. Tag format: [@agent_id: message] (preferred, supports multiple)
        2. Bare format: @agent_id (fallback, first match only)

        Args:
            response: Agent response text.
            current_agent_id: Current agent (excluded from matches).
            team_id: Team ID to validate teammates.

        Returns:
            List of TeammateMention objects.
        """
        results: list[TeammateMention] = []
        seen: set[str] = set()

        # Try tag format first: [@agent_id: message]
        tag_pattern = re.compile(r"\[@(\S+?):\s*([\s\S]*?)\]")
        for match in tag_pattern.finditer(response):
            candidate = match.group(1).lower()
            if candidate not in seen and self.is_teammate(candidate, current_agent_id, team_id):
                results.append(TeammateMention(agent_id=candidate, message=match.group(2).strip()))
                seen.add(candidate)

        if results:
            return results

        # Fallback: bare @mention (first valid match only)
        bare_pattern = re.compile(r"@(\S+)")
        for match in bare_pattern.finditer(response):
            candidate = match.group(1).lower()
            if self.is_teammate(candidate, current_agent_id, team_id):
                return [TeammateMention(agent_id=candidate, message=response)]

        return []
