"""Agent invocation via CLI subprocess delegation."""

import asyncio
import json
from pathlib import Path

from loguru import logger

from flowly.config.schema import MultiAgentConfig


# CLI install hints for error messages
INSTALL_HINTS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
    "gemini": "npm install -g @anthropic-ai/gemini-cli",
    "opencode": "brew install opencode  OR  go install github.com/opencode-ai/opencode@latest",
    "droid": "npm install -g @anthropic-ai/factory",
}

# Short name → full model ID mappings
CLAUDE_MODELS = {
    "sonnet": "claude-sonnet-4-5",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
}

CODEX_MODELS = {
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.2": "gpt-5.2",
}


def resolve_claude_model(short_name: str) -> str:
    """Resolve short model name to full Claude model ID."""
    return CLAUDE_MODELS.get(short_name, short_name)


def resolve_codex_model(short_name: str) -> str:
    """Resolve short model name to full Codex model ID."""
    return CODEX_MODELS.get(short_name, short_name)


def parse_codex_jsonl(output: str) -> str:
    """Parse Codex JSONL output and extract the final agent_message."""
    response = ""
    for line in output.strip().split("\n"):
        try:
            data = json.loads(line)
            if data.get("type") == "item.completed" and data.get("item", {}).get("type") == "agent_message":
                response = data["item"].get("text", "")
        except (json.JSONDecodeError, KeyError):
            continue
    return response or "Sorry, I could not generate a response."


def _build_system_context(agent_id: str, workspace_path: Path) -> str:
    """Build system prompt context from agent's AGENTS.md file.

    Reads the AGENTS.md from the agent directory and returns it as
    system prompt context for the subprocess.
    """
    agents_md = workspace_path / agent_id / "AGENTS.md"
    if agents_md.exists():
        return agents_md.read_text()
    return ""


async def invoke_agent(
    agent: MultiAgentConfig,
    agent_id: str,
    message: str,
    workspace_path: Path,
    continue_conversation: bool = True,
    timeout: int = 1800,
) -> str:
    """Invoke an agent via CLI subprocess.

    Args:
        agent: Agent configuration.
        agent_id: Agent identifier.
        message: Message to send to the agent.
        workspace_path: Base path for agent working directories.
        continue_conversation: Whether to continue previous conversation.
        timeout: Subprocess timeout in seconds.

    Returns:
        Agent response text.
    """
    provider = agent.provider or "anthropic"

    # Working directory: explicit config path, or user's home directory.
    # The agent dir (~/.flowly/workspace/agents/{id}/) only holds AGENTS.md
    # and .claude/CLAUDE.md — the agent should work in the user's project.
    if agent.working_directory:
        working_dir = str(Path(agent.working_directory).expanduser())
    else:
        working_dir = str(Path.home())

    # Ensure working directory exists
    Path(working_dir).mkdir(parents=True, exist_ok=True)

    # Agent config directory (holds AGENTS.md and .claude/CLAUDE.md)
    agent_dir = str(workspace_path / agent_id)

    if provider == "anthropic":
        args = ["claude", "--dangerously-skip-permissions"]
        model_id = resolve_claude_model(agent.model) if agent.model else None
        if model_id:
            args.extend(["--model", model_id])
        if continue_conversation:
            args.append("-c")

        # Inject teammate context via --append-system-prompt
        system_context = _build_system_context(agent_id, workspace_path)
        if system_context:
            args.extend(["--append-system-prompt", system_context])

        # Add agent dir so Claude Code can read .claude/CLAUDE.md from there
        args.extend(["--add-dir", agent_dir])

        args.extend(["-p", message])

    elif provider == "openai":
        args = ["codex", "exec"]
        if continue_conversation:
            args.extend(["resume", "--last"])
        model_id = resolve_codex_model(agent.model) if agent.model else None
        if model_id:
            args.extend(["--model", model_id])
        args.extend([
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            message,
        ])

    elif provider == "gemini":
        args = ["gemini"]
        if agent.model:
            args.extend(["--model", agent.model])
        args.extend(["-p", message])

    elif provider == "opencode":
        args = ["opencode", "run"]
        if agent.model:
            args.extend(["--model", agent.model])
        args.append(message)

    elif provider == "droid":
        args = ["droid", "exec", "--auto", "high"]
        if agent.model:
            args.extend(["--model", agent.model])
        args.append(message)

    else:
        raise ValueError(f"Unsupported provider: {provider}")

    logger.info(f"Invoking agent @{agent_id} [{provider}/{agent.model}] in {working_dir}")
    return await run_subprocess(args, cwd=working_dir, timeout=timeout, provider=provider)


async def run_subprocess(
    args: list[str],
    cwd: str,
    timeout: int = 1800,
    provider: str = "anthropic",
) -> str:
    """Run a CLI command as subprocess and return stdout.

    Args:
        args: Command and arguments.
        cwd: Working directory.
        timeout: Timeout in seconds.
        provider: Provider name for output parsing.

    Returns:
        Command output (parsed for codex).

    Raises:
        RuntimeError: If command fails or times out.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        cmd = args[0]
        hint = INSTALL_HINTS.get(cmd, f"install '{cmd}' and make sure it's in your PATH")
        raise RuntimeError(f"Command '{cmd}' not found. Install it first: {hint}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Agent subprocess timed out after {timeout}s")

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() or f"Process exited with code {proc.returncode}"
        raise RuntimeError(f"Agent process failed: {error_msg}")

    output = stdout.decode()

    if provider == "openai":
        return parse_codex_jsonl(output)

    return output.strip()
