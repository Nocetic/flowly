"""CLI commands — agent_cmd."""

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from flowly import __version__, __logo__

console = Console()

# ============================================================================
# Agent Commands
# ============================================================================


def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from flowly.config.loader import load_config, get_data_dir
    from flowly.bus.queue import MessageBus
    from flowly.providers.factory import build_provider
    from flowly.agent.loop import AgentLoop
    from flowly.cron.service import CronService

    config = load_config()

    # Materialize bundled skills into ~/.flowly/skills so the agent can find and
    # run their scripts (manifest-tracked, preserves user edits; cheap no-op once
    # synced).
    from flowly.skills.sync import ensure_synced
    ensure_synced(quiet=True)

    from flowly.integrations.active_provider import resolve_active_provider
    active = resolve_active_provider(config)
    if active is None:
        console.print("[red]Error: No LLM provider available.[/red]")
        console.print("Run `flowly setup` to pick a provider (or `flowly login`).")
        raise typer.Exit(1)

    fallback_keys: list[str] = []
    if active.key != "flowly":
        provider_cfg = getattr(config.providers, active.key, None)
        if provider_cfg is not None:
            fallback_keys = getattr(provider_cfg, "fallback_keys", []) or []

    bus = MessageBus()
    provider = build_provider(
        active,
        default_model=config.agents.defaults.model,
        fallback_keys=fallback_keys,
        config=config,
    )

    # Create cron service for agent CLI
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Build compaction config
    from flowly.compaction.types import CompactionConfig, MemoryFlushConfig
    compaction_cfg = config.agents.defaults.compaction
    compaction_config = CompactionConfig(
        mode=compaction_cfg.mode,
        reserve_tokens_floor=compaction_cfg.reserve_tokens_floor,
        max_history_share=compaction_cfg.max_history_share,
        context_window=compaction_cfg.context_window,
        memory_flush=MemoryFlushConfig(
            enabled=compaction_cfg.memory_flush.enabled,
            soft_threshold_tokens=compaction_cfg.memory_flush.soft_threshold_tokens,
            prompt=compaction_cfg.memory_flush.prompt,
            system_prompt=compaction_cfg.memory_flush.system_prompt,
        ),
    )

    # Build exec config
    from flowly.exec.types import ExecConfig
    exec_cfg = config.tools.exec
    # security/ask come from the approvals store, not config.json — see
    # ExecToolConfig docstring. Only the runtime knobs flow through here.
    exec_config = ExecConfig(
        enabled=exec_cfg.enabled,
        timeout_seconds=exec_cfg.timeout_seconds,
        max_output_chars=exec_cfg.max_output_chars,
        approval_timeout_seconds=exec_cfg.approval_timeout_seconds,
    )

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        action_temperature=config.agents.defaults.action_temperature,
        action_tool_retries=config.agents.defaults.action_tool_retries,
        brave_api_key=config.tools.web.search.api_key or None,
        cron_service=cron,
        context_messages=config.agents.defaults.context_messages,
        compaction_config=compaction_config,
        exec_config=exec_config,
        trello_config=config.integrations.trello,
        voice_config=config.integrations.voice,
        x_config=config.integrations.x,
        persona=config.agents.defaults.persona,
        memory_search_config=config.agents.defaults.memory_search,
        state_dir=get_data_dir(),
        main_config=config,
    )

    async def handle_compact(instructions: str | None = None) -> None:
        """Handle /compact command."""
        console.print("[cyan]⚙️ Compacting conversation history...[/cyan]")
        result = await agent_loop.compact_session(session_id, instructions)
        if result["success"]:
            console.print(
                f"[green]✓[/green] {result['message']} "
                f"({result['tokens_before']} → {result['tokens_after']} tokens)"
            )
            console.print(f"\n[dim]Summary preview:[/dim]\n{result['summary_preview']}")
        else:
            console.print(f"[yellow]{result['message']}[/yellow]")

    def _format_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    def _display_tool_results(meta: dict) -> None:
        """Show tool execution summary from metadata."""
        tool_results = meta.get("tool_results", [])
        if not tool_results:
            return
        for tr in tool_results:
            name = tr.get("tool", "?")
            ok = tr.get("success", False)
            icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
            result_preview = tr.get("result", "")
            if len(result_preview) > 120:
                result_preview = result_preview[:120] + "..."
            result_preview = result_preview.replace("\n", " ")
            console.print(f"  {icon} [cyan]{name}[/cyan] [dim]{result_preview}[/dim]")

    def _display_usage(meta: dict) -> None:
        """Show token usage from metadata."""
        usage = meta.get("usage", {})
        total = usage.get("total_tokens", 0)
        if total > 0:
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            console.print(
                f"  [dim]tokens: {_format_tokens(prompt)} in + "
                f"{_format_tokens(completion)} out = {_format_tokens(total)}[/dim]"
            )

    def _show_status_bar() -> None:
        """Display model/session/persona info."""
        model_name = config.agents.defaults.model or "unknown"
        persona_name = config.agents.defaults.persona or "default"
        console.print(
            f"[dim]{model_name} | session: {session_id} | persona: {persona_name}[/dim]"
        )

    if message:
        # Single message mode - check for /compact
        if message.strip().startswith("/compact"):
            parts = message.strip().split(" ", 1)
            instructions = parts[1] if len(parts) > 1 else None
            asyncio.run(handle_compact(instructions))
        else:
            async def run_once():
                response, meta = await agent_loop.process_direct(
                    message, session_id, return_metadata=True
                )
                _display_tool_results(meta)
                console.print(f"\n{__logo__} {response}")
                _display_usage(meta)
            asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)")
        _show_status_bar()
        console.print("[dim]Commands: /help for all commands[/dim]\n")

        async def run_interactive():
            nonlocal session_id
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    # Handle slash commands
                    if user_input.strip().startswith("/"):
                        cmd_parts = user_input.strip().split(" ", 1)
                        cmd = cmd_parts[0].lower()
                        args = cmd_parts[1] if len(cmd_parts) > 1 else None

                        if cmd == "/compact":
                            await handle_compact(args)
                            continue
                        elif cmd == "/clear":
                            session = agent_loop.sessions.get_or_create(session_id)
                            session.clear()
                            agent_loop.sessions.save(session)
                            console.print("[green]✓[/green] Session cleared")
                            continue
                        elif cmd in ("/quit", "/exit", "/q"):
                            console.print("Goodbye!")
                            break
                        elif cmd == "/status":
                            _show_status_bar()
                            session = agent_loop.sessions.get_or_create(session_id)
                            msg_count = len(session.messages)
                            console.print(f"[dim]messages in session: {msg_count}[/dim]")
                            continue
                        elif cmd == "/model":
                            if args:
                                agent_loop.model = args.strip()
                                console.print(f"[green]✓[/green] Model set to [cyan]{args.strip()}[/cyan]")
                            else:
                                console.print(f"[cyan]Current model:[/cyan] {agent_loop.model}")
                            continue
                        elif cmd == "/session":
                            if args:
                                session_id = args.strip()
                                console.print(f"[green]✓[/green] Session set to [cyan]{session_id}[/cyan]")
                            else:
                                console.print(f"[cyan]Current session:[/cyan] {session_id}")
                            continue
                        elif cmd == "/sessions":
                            all_sessions = agent_loop.sessions.list_sessions()
                            if not all_sessions:
                                console.print("[dim]No sessions[/dim]")
                            else:
                                for s in all_sessions[:20]:
                                    key = s.get("key", "?")
                                    marker = " [green]*[/green]" if key == session_id else ""
                                    console.print(f"  {key}{marker}")
                            continue
                        elif cmd == "/tasks":
                            from flowly.agent.subagent_registry import SubagentRegistry
                            registry = SubagentRegistry()
                            _render_sessions_table(registry.all())
                            continue
                        elif cmd == "/help":
                            console.print("\n[bold]Available commands:[/bold]")
                            console.print("  /compact [instructions] - Summarize conversation history")
                            console.print("  /clear                  - Clear session history")
                            console.print("  /status                 - Show model, session, persona info")
                            console.print("  /model [name]           - Show or set current model")
                            console.print("  /session [key]          - Show or switch session")
                            console.print("  /sessions               - List all sessions")
                            console.print("  /tasks                  - List background subagent tasks")
                            console.print("  /quit                   - Exit interactive mode")
                            console.print("  /help                   - Show this help\n")
                            continue

                    response, meta = await agent_loop.process_direct(
                        user_input, session_id, return_metadata=True
                    )
                    _display_tool_results(meta)
                    console.print(f"\n{__logo__} {response}")
                    _display_usage(meta)
                    console.print()
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())
