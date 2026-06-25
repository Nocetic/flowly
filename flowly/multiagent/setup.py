"""Agent directory setup — creates working directories and config files."""

from pathlib import Path

from loguru import logger

from flowly.config.schema import MultiAgentConfig, MultiAgentTeamConfig


AGENTS_MD_TEMPLATE = """\
# Flowly Multi-Agent System

You are running inside Flowly's multi-agent orchestration system.

## IMPORTANT: Team Communication

You have a WORKING communication channel with your teammates. \
When you include the tag `[@agent_id: message]` in your response, \
the Flowly orchestrator WILL deliver that message to the teammate agent and \
return their response. This is NOT hypothetical — it is a real, functional system.

**YOU MUST USE THIS** when a task requires another teammate's skills. \
Do NOT tell the user you cannot communicate with teammates. You CAN and SHOULD.

### How to message a teammate

Include the tag anywhere in your response. The orchestrator intercepts it automatically.

**Single teammate:**
```
[@coder: Can you fix the login bug in auth.ts?]
```

**Multiple teammates (parallel fan-out):**
```
[@coder: Fix the auth bug in login.ts] [@reviewer: Review the PR for security issues]
```
All mentioned teammates are invoked in parallel and their responses are collected.

**Back-and-forth chain:**
When you mention a teammate, they receive your message and can mention you back. \
The orchestrator routes messages in real-time until the chain completes.

### Rules

- Always use the exact tag format: `[@agent_id: your message here]`
- The agent_id must match one of your teammates listed below
- You can include multiple tags in one response for parallel work
- Your teammate's response will be appended to the conversation

<!-- TEAMMATES_START -->
<!-- TEAMMATES_END -->
"""


def ensure_agent_directory(
    agent_dir: Path,
    agent_id: str,
    agents: dict[str, MultiAgentConfig],
    teams: dict[str, MultiAgentTeamConfig],
) -> None:
    """Create or update agent workspace with config files.

    Args:
        agent_dir: Path to agent's working directory.
        agent_id: Agent identifier.
        agents: All configured agents.
        teams: All configured teams.
    """
    if agent_dir.exists():
        # Directory exists — just update teammate info
        update_agent_teammates(agent_dir, agent_id, agents, teams)
        return

    logger.info(f"Initializing agent directory: {agent_dir}")
    agent_dir.mkdir(parents=True)

    # Create .claude/ directory with CLAUDE.md
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir()

    # Write AGENTS.md with team communication instructions
    (agent_dir / "AGENTS.md").write_text(AGENTS_MD_TEMPLATE)

    # Update CLAUDE.md with teammate info
    update_agent_teammates(agent_dir, agent_id, agents, teams)


def update_agent_teammates(
    agent_dir: Path,
    agent_id: str,
    agents: dict[str, MultiAgentConfig],
    teams: dict[str, MultiAgentTeamConfig],
) -> None:
    """Update CLAUDE.md and AGENTS.md with current teammate information.

    Args:
        agent_dir: Path to agent's working directory.
        agent_id: Agent identifier.
        agents: All configured agents.
        teams: All configured teams.
    """
    # Collect teammates from all teams this agent belongs to
    teammates: list[tuple[str, MultiAgentConfig]] = []
    seen: set[str] = set()

    for team in teams.values():
        if agent_id not in team.agents:
            continue
        for tid in team.agents:
            if tid != agent_id and tid in agents and tid not in seen:
                teammates.append((tid, agents[tid]))
                seen.add(tid)

    # Build teammate section
    self_agent = agents.get(agent_id)
    block = ""
    if self_agent:
        block += f"\n### You\n\n- `@{agent_id}` — **{self_agent.name or agent_id}** ({self_agent.model})\n"
    if teammates:
        block += "\n### Your Teammates\n\n"
        block += "You can message them using `[@agent_id: message]` tag format. This WORKS — the orchestrator delivers it.\n\n"
        for tid, cfg in teammates:
            block += f"- `@{tid}` — **{cfg.name or tid}** ({cfg.model})\n"

    # Update AGENTS.md between markers
    agents_md = agent_dir / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text()
        start_marker = "<!-- TEAMMATES_START -->"
        end_marker = "<!-- TEAMMATES_END -->"
        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)
        if start_idx != -1 and end_idx != -1:
            new_content = (
                content[: start_idx + len(start_marker)]
                + block
                + content[end_idx:]
            )
            agents_md.write_text(new_content)

    # Write/update .claude/CLAUDE.md
    claude_md = agent_dir / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True, exist_ok=True)

    claude_content = ""
    start_marker = "<!-- TEAMMATES_START -->"
    end_marker = "<!-- TEAMMATES_END -->"

    if claude_md.exists():
        claude_content = claude_md.read_text()
        start_idx = claude_content.find(start_marker)
        end_idx = claude_content.find(end_marker)
        if start_idx != -1 and end_idx != -1:
            claude_content = (
                claude_content[: start_idx + len(start_marker)]
                + block
                + claude_content[end_idx:]
            )
        else:
            claude_content = claude_content.rstrip() + "\n\n" + start_marker + block + end_marker + "\n"
    else:
        claude_content = (
            f"# Agent: @{agent_id}\n\n"
            "You are part of a multi-agent team. "
            "To delegate work to a teammate, use `[@agent_id: message]` in your response. "
            "The Flowly orchestrator will deliver the message and return their response.\n\n"
            f"{start_marker}{block}{end_marker}\n"
        )

    claude_md.write_text(claude_content)
