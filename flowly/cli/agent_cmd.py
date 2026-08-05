"""CLI commands — agent_cmd."""

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

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
    from flowly.compaction.builder import build_compaction_config
    compaction_config = build_compaction_config(config.agents.defaults.compaction)

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

    # Set while a user-requested /compact runs, so the automatic-compaction
    # hook below does not narrate a compaction the user is already watching.
    _manual_compact_in_flight = {"value": False}

    async def handle_compact(instructions: str | None = None) -> None:
        """Handle /compact command."""
        console.print("[cyan]⚙️ Compacting conversation history...[/cyan]")
        _manual_compact_in_flight["value"] = True
        try:
            result = await agent_loop.compact_session(session_id, instructions)
        finally:
            _manual_compact_in_flight["value"] = False
        if result["success"]:
            console.print(
                f"[green]✓[/green] {result['message']} "
                f"({result['tokens_before']} → {result['tokens_after']} tokens)"
            )
            console.print(f"\n[dim]Summary preview:[/dim]\n{result['summary_preview']}")
        else:
            console.print(f"[yellow]{result['message']}[/yellow]")

    # Automatic compaction was invisible here: the notification hook is wired
    # by the gateway, and the CLI runs the agent in-process. So a conversation
    # would stall for many seconds mid-turn with nothing on screen to say why.
    _compaction_spinner: dict[str, Any] = {"status": None, "cycle": ""}

    def _stop_compaction_spinner() -> None:
        status = _compaction_spinner.get("status")
        if status is not None:
            try:
                status.stop()
            except Exception:  # noqa: BLE001 — cosmetic
                pass
        _compaction_spinner["status"] = None

    async def _on_cli_compaction(
        session_key: str,
        tokens_before: int,
        tokens_after: int,
        messages_removed: int,
        phase: str = "completed",
        compaction_id: str = "",
    ) -> None:
        if session_key and session_key != session_id:
            return
        if phase == "started":
            # The manual /compact path prints its own progress; announcing
            # again would double every line.
            _compaction_spinner["cycle"] = compaction_id
            if _manual_compact_in_flight["value"]:
                return
            status = console.status(
                "[cyan]Compacting context…[/cyan]", spinner="dots"
            )
            status.start()
            _compaction_spinner["status"] = status
            return
        # A terminal closes the cycle it belongs to — an event from another
        # pass (a retry, a reorder) must not close the notice on screen and
        # report its own numbers.
        cycle = _compaction_spinner.get("cycle") or ""
        if compaction_id and cycle and compaction_id != cycle:
            return
        _compaction_spinner["cycle"] = ""
        _stop_compaction_spinner()
        if _manual_compact_in_flight["value"]:
            return
        if phase == "failed":
            console.print(
                "[yellow]⚠ context compaction failed — history kept[/yellow]"
            )
            return
        saved = max(0, tokens_before - tokens_after)
        console.print(
            f"[dim]⚡ context compacted · {messages_removed} messages"
            + (f" · −{saved:,} tokens" if saved else "")
            + "[/dim]"
        )

    agent_loop._on_compaction = _on_cli_compaction

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

    def _display_media(meta: dict) -> None:
        """List the files this turn produced, by absolute path.

        A terminal cannot play a clip, so the useful thing is to say exactly
        where it landed. The paths come from the turn's metadata rather than
        being parsed out of the reply text, and a file that is not actually on
        disk is not listed — telling someone a video is ready when it isn't is
        worse than saying nothing.
        """
        paths = [p for p in (meta.get("media") or []) if isinstance(p, str)]
        if not paths:
            return

        from flowly.media.assets import assets_from_meta, index_by_path

        assets = index_by_path(assets_from_meta(meta.get("media_assets")))
        shown = 0
        for raw in paths:
            if raw.startswith(("http://", "https://")):
                continue
            path = Path(raw)
            if not path.is_file():
                continue
            if shown == 0:
                console.print("\n[dim]Saved:[/dim]")
            shown += 1
            asset = assets.get(raw)
            kind = (asset.kind if asset else "") or "file"
            console.print(f"  [cyan]{kind}[/cyan] {path.resolve()}")
            poster = getattr(asset, "poster_path", None)
            if poster and Path(poster).is_file():
                console.print(f"  [dim]poster[/dim] {Path(poster).resolve()}")

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
                _display_media(meta)
                _display_usage(meta)
                # A one-shot run ends when this coroutine returns, and
                # asyncio.run() then cancels whatever is still pending —
                # including the post-turn compaction this turn just scheduled.
                # So the CLI got the check but never the work: the session
                # stayed over budget and the NEXT invocation paid for it.
                await agent_loop.await_post_turn_compaction(session_id)
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
                            # One entry point: it drops the compaction summary
                            # (plain clear() leaves it in metadata and the
                            # summary anchor re-injects it next turn) AND bumps
                            # the context epoch, which is what stops a
                            # compaction already in flight from committing the
                            # old conversation over the empty one.
                            removed = agent_loop.reset_conversation(session_id)
                            console.print(
                                f"[green]✓[/green] Session cleared ({removed} messages)"
                            )
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
                                # Compaction budgets and counts tokens against
                                # the ACTIVE model — leaving them on the old one
                                # sizes the context window and the per-message
                                # overheads for a model that is no longer in use.
                                agent_loop.compaction.model = agent_loop.model
                                from flowly.compaction.estimator import set_active_model

                                set_active_model(agent_loop.model)
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
                    _display_media(meta)
                    _display_usage(meta)
                    console.print()
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())
