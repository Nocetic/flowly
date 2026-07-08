"""CLI commands — gateway_cmd."""

import asyncio
import os
import platform
import signal
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

import typer
from rich.console import Console
from rich.table import Table

from flowly import __version__, __logo__

console = Console()

_GATEWAY_FILE_SINK_ID: int | None = None


def _schedule_cron_push_notification(
    job, response: str | None, *, conversation_id: str = ""
) -> None:
    """Best-effort APNs/FCM notification for a completed gateway cron run.

    Targeted cron deliveries pass ``conversation_id`` so a tap can open the
    chat. Jobs created from the remote gateway Schedule screen may have no chat
    target; they still need a banner, but with no deep-link conversation.
    """
    preview = next(
        (ln.strip() for ln in (response or "").splitlines() if ln.strip()),
        getattr(job, "name", None) or "Scheduled task",
    )[:140]
    title = getattr(job, "name", None) or "Flowly"
    data = {
        "type": "cron",
        "jobId": str(getattr(job, "id", "") or ""),
        "jobName": str(getattr(job, "name", "") or ""),
    }
    data = {k: v for k, v in data.items() if v}

    async def _run() -> None:
        try:
            from flowly.push.relay_push import notify_devices

            await notify_devices(
                title,
                preview,
                conversation_id=conversation_id,
                data=data,
            )
        except Exception as exc:  # pragma: no cover - best-effort background notify
            logger.debug(
                f"Cron '{getattr(job, 'name', '')}' push-notify skipped: {exc}"
            )

    asyncio.create_task(_run())


def _install_gateway_file_sink(level: str = "INFO") -> None:
    """Install a bot-side daily-rotating file sink for gateway logs.

    Additive — the default loguru stderr sink stays, so the service manager
    still captures stdout/stderr as before. This guarantees day-by-day local
    operational logs (``~/.flowly/logs/gateway.log`` + dated ``.gz`` archives,
    30-day retention) regardless of service manager (launchd/systemd/Windows),
    and survives background mode where the inherited stdio pipes are ignored.

    ``enqueue=False`` is intentional: on Python 3.14 loguru's enqueue worker
    fork()s and inherits exotic high-numbered fds, tripping posix fd
    validation — the same reason the auth audit sink uses enqueue=False.
    Loguru stays thread-safe via an internal lock; the only cost is sink
    writes happen on the calling thread.
    """
    global _GATEWAY_FILE_SINK_ID
    if _GATEWAY_FILE_SINK_ID is not None:
        return
    try:
        from flowly.cli.service_cmd import _get_log_dir
        log_dir = _get_log_dir()
        _GATEWAY_FILE_SINK_ID = logger.add(
            str(log_dir / "gateway.log"),
            rotation="00:00",          # new file at midnight
            retention="30 days",
            compression="gz",
            enqueue=False,             # avoid multiprocessing fork on Python 3.14
            backtrace=False,
            diagnose=False,            # never dump locals (may hold secrets)
            level=level,
        )
    except Exception as exc:  # noqa: BLE001 — logging setup must never block boot
        logger.warning(f"[gateway] daily file log sink not installed: {exc}")


def _should_drop_stderr_sink(stream) -> bool:
    """True when stderr is redirected (a service manager) rather than a terminal.

    Under launchd/systemd the gateway's stderr is ``flowly-gateway.err.log``,
    which the OS appends to forever (no rotation). loguru's default stderr sink
    would duplicate the ENTIRE INFO stream into that unrotated file → unbounded
    disk growth on an always-on bot. The rotated ``gateway.log`` already has the
    full log, so when stderr isn't a TTY we drop the default sink: ``.err.log``
    then only holds raw crash tracebacks (tiny). A foreground ``flowly gateway``
    keeps its console output.
    """
    try:
        return not (getattr(stream, "isatty", None) and stream.isatty())
    except Exception:
        return True


# ============================================================================


def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    persona: str = typer.Option("", "--persona", help="Bot persona (default, jarvis, pirate, samurai, casual, professor, butler, friday)"),
    host: str = typer.Option("", "--host", help="Bind address. Use 0.0.0.0 (or the VPS IP) to accept remote desktop clients. Overrides config.gateway.host (default 127.0.0.1)."),
    remote: bool = typer.Option(False, "--remote", help="Accept connections from your phone / other devices — plain-language alias for --host 0.0.0.0 (a token is ensured automatically)."),
    token: str = typer.Option("", "--token", help="Set an explicit remote-access token (persisted). Otherwise one is auto-generated on first non-loopback bind."),
    rotate_token: bool = typer.Option(False, "--rotate-token", help="Generate a fresh remote-access token before starting (invalidates the old one), print it, and persist it."),
):
    """Start the flowly gateway."""
    # Windows: make stdout/stderr encode the Unicode glyphs we print (✓, →, the
    # banner) without crashing. Run headless or redirected to a log file (Task
    # Scheduler service), Python defaults to the legacy code page (cp1252) and
    # rich's console.print raises UnicodeEncodeError, aborting startup. Force
    # UTF-8 with replacement so output degrades to "?" instead of killing the
    # gateway. Belt-and-braces alongside PYTHONUTF8=1 in the service wrapper.
    if platform.system() == "Windows":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    from flowly.config.loader import load_config, get_data_dir
    from flowly.bus.queue import MessageBus
    from flowly.providers.factory import build_provider
    from flowly.agent.loop import AgentLoop
    from flowly.channels.manager import ChannelManager
    from flowly.cron.service import CronService, is_silent_response
    from flowly.cron.types import CronJob
    from flowly.cron import script_runner, skill_loader
    from flowly.cron.context import cron_context

    # Tools hidden from the agent during cron runs.
    #
    # `cron` / `message` are removed during cron runs — no recursive
    # job scheduling, no direct DMs (delivery is handled by the gateway).
    #
    # The rest are side-effect integrations (Drive, Gmail, Linear, etc.)
    # that the agent otherwise tends to reach for when producing a
    # "deliverable" — even when the job message just asks for a summary.
    # Users can still hit these manually from chat; a scheduled run just
    # shouldn't silently create Docs or send emails behind their back.
    # If a cron genuinely needs one of these, it should use a `script`
    # (which runs with full system privileges on the bot machine).
    CRON_DISABLED_TOOLS = [
        # cron/message are structural — a scheduled run must never recursively
        # schedule more jobs or DM the user directly (delivery is the
        # gateway's job). Keeping these blocked is non-negotiable.
        "cron",
        "message",
        # Lower-level fire-and-forget spawners are hidden so the cron agent
        # reaches for `builtin_agent` (sync in cron context) instead. If
        # cron called `spawn`, it would get "accepted" back and spin waiting
        # for an async announcement that never arrives inline.
        "spawn",
        "delegate_to",
        # NOTE: Side-effect integrations (google_drive, email, linear, etc.)
        # are NOT blocked here by default. If the user says "every morning
        # draft a Gmail reply" the cron must be able to call gmail. The
        # cron system prompt (built in on_cron_job below) steers the agent
        # AWAY from these unless the task explicitly asks — that's softer
        # and more user-respecting than a blanket blacklist.
    ]
    from flowly.heartbeat.service import HeartbeatService
    from flowly.gateway.server import GatewayServer

    import logging
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # Bot-side daily file sink (additive). Under a service manager we then drop
    # loguru's default stderr sink, because stderr is redirected to the
    # never-rotated flowly-gateway.err.log and would duplicate the whole log
    # there (unbounded disk growth). The rotated gateway.log keeps the full log;
    # .err.log is left to capture only raw crash output.
    _install_gateway_file_sink(level="DEBUG" if verbose else "INFO")
    if _GATEWAY_FILE_SINK_ID is not None and _should_drop_stderr_sink(sys.stderr):
        try:
            logger.remove(0)  # loguru's default stderr handler
        except (ValueError, OSError):
            pass

    from flowly import __banner__
    console.print(f"[cyan]{__banner__.format(version=__version__)}[/cyan]")
    console.print(f"Starting gateway on port {port}...")

    # Load .env from data dir (contains MOLTBOT_PROXY_JWT_SECRET, API keys, etc.)
    data_dir = get_data_dir()
    env_file = data_dir / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

    config = load_config()

    # ── Remote access: bind host + auth token resolution ──────────────────
    # --host overrides the configured bind address. Loopback (127.0.0.1) needs
    # no auth; binding to a non-loopback host exposes the gateway to the
    # network, so a token is required — we auto-generate + persist + print one
    # if none is set, mirroring how a self-hosted server hands you a key.
    from flowly.gateway.auth import generate_gateway_token, is_loopback_host
    from flowly.config.loader import save_config

    # --remote is the friendly alias for --host 0.0.0.0; an explicit --host
    # still wins, otherwise fall back to the configured bind address.
    if host.strip():
        effective_host = host.strip()
    elif remote:
        effective_host = "0.0.0.0"
    else:
        effective_host = config.gateway.host
    auth_token = (config.gateway.token or "").strip()
    remote_exposed = not is_loopback_host(effective_host)

    _token_changed = False
    if token.strip():
        auth_token = token.strip()
        _token_changed = True
    elif rotate_token:
        auth_token = generate_gateway_token()
        _token_changed = True
    elif remote_exposed and not auth_token:
        auth_token = generate_gateway_token()
        _token_changed = True

    if _token_changed or (host.strip() and config.gateway.host != effective_host):
        config.gateway.token = auth_token
        if host.strip():
            config.gateway.host = effective_host
        try:
            save_config(config)
        except Exception as e:  # pragma: no cover — never block startup
            logger.warning(f"[Gateway] Could not persist gateway config: {e}")

    if remote_exposed and auth_token:
        _bind_all = effective_host in ("0.0.0.0", "::")
        console.print(
            "\n[bold yellow]Remote access enabled[/bold yellow] — from the desktop, "
            "Settings → Connection → Your server:"
        )
        if _bind_all:
            console.print(
                "  Host/IP : [bold]<this server's public IP>[/bold]   "
                "[dim](listening on all interfaces; find it with `curl -s ifconfig.me`)[/dim]"
            )
        else:
            console.print(f"  Host/IP : [bold]{effective_host}[/bold]")
        console.print(f"  Port    : [bold]{port}[/bold]")
        console.print(f"  Token   : [bold]{auth_token}[/bold]")
        console.print(
            "[dim]The token is secret (rotate with `flowly gateway --rotate-token`). "
            "Expose only over TLS (reverse proxy) or a private network (Tailscale/VPN).[/dim]\n"
        )

        # Same values as a scannable code. Use the LAN IP when bound to all
        # interfaces (instant, no network call — never slow gateway startup);
        # an explicit --host is encoded as-is.
        from flowly.gateway.remote_info import detect_lan_ip
        from flowly.gateway.remote_qr import remote_qr_markup

        qr_host = detect_lan_ip() if _bind_all else effective_host
        qr = remote_qr_markup(qr_host, port, auth_token) if qr_host else None
        if qr:
            console.print(f"  [bold]Scan with the Flowly app[/bold] [dim]({qr_host}:{port})[/dim]\n")
            console.print(qr)
            console.print()

    # Prune the audit log before anything else writes to it. Best-effort —
    # never blocks startup if the disk / filesystem is misbehaving.
    if config.audit.enabled:
        try:
            from flowly.audit.retention import prune_audit_logs
            from flowly.profile import get_flowly_home
            prune_audit_logs(
                audit_dir=get_flowly_home() / "audit",
                retention_days=config.audit.retention_days,
                max_size_mb=config.audit.max_size_mb,
            )
        except Exception as e:
            logger.debug(f"[Gateway] Audit retention skipped: {e}")

    # Prune generated media (~/.flowly/media) so image generation can't fill the
    # disk over time — the disk-cleanup plugin deliberately protects this folder,
    # so nothing else reclaims it. Best-effort; never blocks startup. Tunable via
    # FLOWLY_MEDIA_RETENTION_DAYS / FLOWLY_MEDIA_MAX_SIZE_MB (age -1 / size 0 = off).
    try:
        from flowly.media.retention import (
            DEFAULT_MAX_SIZE_MB,
            DEFAULT_RETENTION_DAYS,
            prune_media,
        )
        from flowly.profile import get_flowly_home

        def _media_env_int(name: str, default: int) -> int:
            try:
                return int(os.environ[name])
            except (KeyError, ValueError):
                return default

        prune_media(
            get_flowly_home() / "media",
            retention_days=_media_env_int("FLOWLY_MEDIA_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
            max_size_mb=_media_env_int("FLOWLY_MEDIA_MAX_SIZE_MB", DEFAULT_MAX_SIZE_MB),
        )
    except Exception as e:
        logger.debug(f"[Gateway] Media retention skipped: {e}")

    # Resolve persona: CLI flag overrides config
    active_persona = persona if persona else config.agents.defaults.persona
    if active_persona:
        console.print(f"[dim]Persona: {active_persona}[/dim]")

    # Create components
    bus = MessageBus()

    # Pick the active LLM provider: Flowly hosted (signed-in account)
    # wins when enabled; otherwise the BYOK priority cascade. The choice
    # is centralised in resolve_active_provider() so the integrations
    # modal and the gateway agree on what's running.
    from flowly.integrations.active_provider import resolve_active_provider
    active = resolve_active_provider(config)
    if active is None:
        console.print("[red]Error: No LLM provider available.[/red]")
        console.print(
            "Configure one first — the gateway can't start without a provider:"
        )
        console.print(
            "  [bold]flowly setup[/bold]                          "
            "[dim]— pick a provider (or sign in with `flowly login`)[/dim]"
        )
        console.print(
            "  [bold]flowly setup byok <provider> --key <KEY>[/bold]  "
            "[dim]— one-shot CLI (openrouter / anthropic / openai / …)[/dim]"
        )
        raise typer.Exit(1)

    console.print(f"[dim]LLM provider: {active.source}[/dim]")

    # Fallback keys only make sense for BYOK; Flowly's auth is a single
    # refreshable Firebase token, not a rotation pool.
    fallback_keys: list[str] = []
    if active.key != "flowly":
        provider_cfg = getattr(config.providers, active.key, None)
        if provider_cfg is not None:
            fallback_keys = getattr(provider_cfg, "fallback_keys", []) or []

    provider = build_provider(
        active,
        default_model=config.agents.defaults.model,
        fallback_keys=fallback_keys,
        config=config,
    )

    # Create cron service first (agent needs it)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Build compaction config from settings
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

    # Sync bundled skills — manifest-based, respects user edits
    from flowly.skills.sync import ensure_synced
    ensure_synced(quiet=True)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        action_temperature=config.agents.defaults.action_temperature,
        action_tool_retries=config.agents.defaults.action_tool_retries,
        max_iterations=config.agents.defaults.max_tool_iterations,
        soft_warn_at_iteration=config.agents.defaults.soft_warn_at_iteration,
        brave_api_key=config.tools.web.search.api_key or None,
        cron_service=cron,
        context_messages=config.agents.defaults.context_messages,
        compaction_config=compaction_config,
        exec_config=exec_config,
        trello_config=config.integrations.trello,
        voice_config=config.integrations.voice,
        x_config=config.integrations.x,
        persona=active_persona,
        memory_search_config=config.agents.defaults.memory_search,
        state_dir=get_data_dir(),
        main_config=config,
    )

    # Multi-agent setup (if agents are configured in config.json)
    multi_agents = config.agents.agents
    multi_teams = config.agents.teams

    if multi_agents:
        from flowly.multiagent.router import AgentRouter
        from flowly.multiagent.orchestrator import TeamOrchestrator
        from flowly.multiagent.setup import ensure_agent_directory
        from flowly.agent.tools.delegate import DelegateTool

        ma_router = AgentRouter(multi_agents, multi_teams)
        ma_orchestrator = TeamOrchestrator(ma_router)

        # Setup agent working directories
        agents_workspace = config.workspace_path / "agents"
        for aid, acfg in multi_agents.items():
            agent_dir = agents_workspace / aid
            ensure_agent_directory(agent_dir, aid, multi_agents, multi_teams)

        # Register delegate_to tool on main agent
        delegate_tool = DelegateTool(multi_agents, multi_teams, agents_workspace, bus)
        agent.tools.register(delegate_tool)

        # Wrap _process_message with multi-agent routing
        _original_process = agent._process_message

        async def _routed_process(msg):
            from flowly.bus.events import InboundMessage as _IB, OutboundMessage as _OB

            # Update delegate tool context so background results go to the right chat
            delegate_tool.set_context(msg.channel, msg.chat_id)

            # System messages bypass routing
            if msg.channel == "system":
                return await _original_process(msg)

            # Background delegate result — model should summarize, NOT re-delegate
            if msg.content.startswith("[DELEGATE_RESULT:"):
                # Temporarily remove delegate_to tool to prevent loops
                agent.tools.unregister("delegate_to")
                try:
                    return await _original_process(msg)
                finally:
                    # Restore the tool for future messages
                    agent.tools.register(delegate_tool)

            # Route @mentions
            routing = ma_router.route(msg.content)

            if routing.agent_id == "default" or routing.agent_id not in multi_agents:
                return await _original_process(msg)

            # @mention detected — bypass LLM, invoke agent directly
            # This ensures the user's message is passed verbatim (no LLM rephrasing)
            agent_cfg = multi_agents[routing.agent_id]
            model_display = delegate_tool._resolve_model(agent_cfg)

            # Fire-and-forget: invoke agent subprocess in background
            await delegate_tool.execute(routing.agent_id, routing.message)

            # Return immediate ack to user — let main LLM phrase it naturally
            msg.content = (
                f"[SYSTEM: You have just delegated a task to @{routing.agent_id} "
                f"({agent_cfg.name or routing.agent_id}, {model_display}). "
                f"The agent is now working in the background. "
                f"Tell the user briefly that the task was delegated and they will be notified when done. "
                f"Do NOT re-delegate or call any tools.]"
            )
            agent.tools.unregister("delegate_to")
            try:
                return await _original_process(msg)
            finally:
                agent.tools.register(delegate_tool)

        agent._process_message = _routed_process

        agent_names = [f"@{aid} ({acfg.name})" for aid, acfg in multi_agents.items()]
        console.print(f"[green]✓[/green] Multi-agent: {', '.join(agent_names)}")
        if multi_teams:
            team_names = [f"@{tid} ({tcfg.name})" for tid, tcfg in multi_teams.items()]
            console.print(f"[green]✓[/green] Teams: {', '.join(team_names)}")

    # Set cron job callback (needs agent to be created first)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        with cron_context():
            return await _on_cron_job_inner(job)

    async def _on_cron_job_inner(job: CronJob) -> str | None:
        """Actual implementation, wrapped by cron_context() above so the
        executor's approval gate and any cron-aware downstream code can
        detect we're inside a scheduled run."""

        async def _notify_error(error_text: str) -> None:
            """Send an error notification to the user if deliver is configured."""
            channel = job.payload.channel or "telegram"
            to = job.payload.to
            if job.payload.deliver and to:
                from flowly.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=to,
                    content=f"⚠️ Scheduled task '{job.name}' failed:\n{error_text}",
                ))

        def _inject_cron_result_to_session(
            job: CronJob, result: str, *, is_error: bool = False
        ) -> None:
            """Inject cron job result into the user's original session so the
            agent is aware of what happened when the user continues chatting."""
            channel = job.payload.channel or "telegram"
            chat_id = job.payload.to
            if not chat_id:
                return
            session_key = f"{channel}:{chat_id}"
            session = agent.sessions.get_or_create(session_key)
            status = "ERROR" if is_error else "COMPLETED"
            session.add_message(
                "system",
                f"[Cron Job {status}: {job.name}]\n{result}",
            )
            agent.sessions.save(session)

        if job.payload.kind == "tool_call":
            tool_name = job.payload.tool_name
            if not tool_name:
                raise ValueError(f"Cron job '{job.id}' is tool_call but tool_name is missing")

            delivery_channel = job.payload.channel or "telegram"
            delivery_to = job.payload.to

            # Rehydrate tool contexts for direct cron-triggered tool execution.
            if delivery_to:
                for context_tool_name in ("message", "spawn", "cron", "voice_call"):
                    context_tool = agent.tools.get(context_tool_name)
                    if context_tool and hasattr(context_tool, "set_context"):
                        context_tool.set_context(delivery_channel, delivery_to)

            try:
                result = await agent.tools.execute(tool_name, job.payload.tool_args or {})
            except Exception as e:
                err = f"Tool '{tool_name}' raised an exception: {e}"
                logger.error(f"Cron job '{job.name}' tool_call failed: {e}")
                _inject_cron_result_to_session(job, err, is_error=True)
                await _notify_error(err)
                return f"__error__:{err}"

            is_error = bool(result and result.startswith("Error"))

            # Inject result into user's session so agent knows what happened
            _inject_cron_result_to_session(job, result or "✓ Done.", is_error=is_error)

            if job.payload.deliver and delivery_to:
                from flowly.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=delivery_channel,
                    chat_id=delivery_to,
                    content=result or "✓ Done.",
                ))

            return result

        # ─── Pre-run script ────────────────────────────────────────────
        # If the job has a `script` field, run it BEFORE the agent turn so
        # the agent sees fresh data. Script stdout is injected into the
        # prompt under a "## Script Output" header; errors become a
        # "## Script Error" section the agent is told to surface. If the
        # script signals `{"wakeAgent": false}` as its last JSON line, we
        # skip the agent turn entirely (no-op data-collection runs).
        script_preamble = ""
        if job.script:
            # Run the blocking subprocess off the event loop — `script_runner.run`
            # does a synchronous `subprocess.run(..., timeout=120)`, so calling it
            # inline would freeze the WHOLE gateway (all channels, WS, REST,
            # heartbeat, other cron) for the script's entire duration. to_thread
            # keeps the loop serving everyone else while the script runs.
            script_result = await asyncio.to_thread(script_runner.run, job.script)
            script_preamble = script_runner.format_for_prompt(script_result)
            if script_result.success and not script_result.wake_agent:
                logger.info(
                    f"Cron job '{job.name}': script returned wakeAgent=false — "
                    f"skipping agent turn"
                )
                return "[SILENT]"

        # ─── Skill preamble ────────────────────────────────────────────
        # Load skill bodies and inject them as [SYSTEM:]-banners ahead of
        # the user prompt. Missing skills are a soft fail — the job still
        # runs with a ⚠️ notice so the agent can surface the gap.
        skill_preamble = ""
        if job.skills:
            try:
                sp = await skill_loader.build_skill_preamble(
                    job.skills, agent.workspace
                )
                skill_preamble = sp.preamble
                if sp.skipped:
                    logger.warning(
                        f"Cron job '{job.name}': skills skipped — {sp.skipped}"
                    )
            except Exception as e:
                logger.error(f"Cron job '{job.name}' skill load failed: {e}")
                skill_preamble = (
                    f"[SYSTEM: Skill loading failed ({e}). "
                    "Start your response with a brief notice to the user.]"
                )

        def _augment(body: str) -> str:
            parts = [p for p in (script_preamble, skill_preamble, body) if p]
            return "\n\n".join(parts)

        # Per-job model override, with config hot-reload for the default.
        # If the job pins a specific model, use it; otherwise re-read the
        # current default from disk so config edits take effect without a
        # gateway restart. agent.model (set at startup) remains the final
        # fallback if the fresh config can't be loaded.
        if job.model:
            model_override = job.model
        else:
            try:
                from flowly.config.loader import load_config
                fresh_cfg = load_config()
                fresh_default = fresh_cfg.agents.defaults.model or None
                model_override = fresh_default if fresh_default != agent.model else None
            except Exception as e:
                logger.debug(f"Cron: config hot-reload failed (non-fatal): {e}")
                model_override = None

        # Build the prompt - if delivery is requested, tell agent to return plain text
        prompt = job.payload.message

        if job.payload.deliver:
            channel = job.payload.channel or "telegram"
            if job.payload.to:
                # Tell agent to return plain text — we handle delivery automatically
                cron_body = (
                    f"[Scheduled Task: {job.name}]\n"
                    f"{job.payload.message}\n\n"
                    "# Delivery contract\n"
                    "Your final response will be delivered automatically as plain text. "
                    "You don't need to use the `message` tool, create Google Docs/Drive "
                    "files, send emails, create calendar events, or open Linear issues "
                    "unless the task ABOVE explicitly asks for it. The user reads your "
                    "text reply — that's the whole deliverable.\n"
                    "\n"
                    "# Specialist dispatch — ONE call, then STOP\n"
                    "`builtin_agent` delegates focused work to a specialist:\n"
                    "- `researcher` — researches AND writes a final markdown report "
                    "(auto-saved as artifact). Use for ANY 'research X and write it up' "
                    "task. It is SELF-CONTAINED.\n"
                    "- `writer` — only when you already have the raw material and just "
                    "need it reshaped.\n"
                    "- `coder` — code review / refactor / debug.\n"
                    "\n"
                    "STRICT EXECUTION PLAN (follow in order, stop when the condition is met):\n"
                    "1. If a single `web_search`/`web_fetch` answers the task, do that, "
                    "then STOP and reply.\n"
                    "2. Otherwise call ONE specialist (researcher for research+write, "
                    "writer for reshape-only, coder for code).\n"
                    "3. When the specialist returns: your NEXT turn MUST be the final "
                    "reply. No more tool calls. Do NOT look up the artifact you just "
                    "got back (you already have its text). Do NOT 'also save to Drive' "
                    "or 'also send an email'. Just output the report to the user and end.\n"
                    "\n"
                    "Forbidden patterns — these waste 3-5 minutes and deliver nothing "
                    "the user asked for:\n"
                    "  ✗ researcher → writer (chain)\n"
                    "  ✗ specialist → artifact(list) → artifact(get) (you already have "
                    "the content)\n"
                    "  ✗ specialist → google_drive(create) (user didn't ask for Drive)\n"
                    "  ✗ specialist → email(send) (user didn't ask to email anyone)\n"
                    "\n"
                    "If any tool fails, include a one-sentence error note in your reply. "
                    "Then STOP."
                )
                cron_prompt = _augment(cron_body)
                try:
                    # Resolve the real (user-facing) delivery coordinates so
                    # tools that capture session context during the cron run
                    # never persist the literal "cron" channel. Origin is
                    # the strongest signal; payload.channel/to is a fallback
                    # for legacy jobs created before origin tracking.
                    origin_channel = (
                        (job.origin.platform if job.origin else None)
                        or job.payload.channel
                    )
                    origin_chat_id = (
                        (job.origin.chat_id if job.origin else None)
                        or job.payload.to
                    )

                    response = await agent.process_direct(
                        cron_prompt,
                        session_key=f"cron:{job.id}",
                        model_override=model_override,
                        disabled_tools=CRON_DISABLED_TOOLS,
                        skip_memory=True,
                        skip_context_files=True,
                        origin_channel=origin_channel,
                        origin_chat_id=origin_chat_id,
                    )
                except Exception as e:
                    err = f"Agent execution failed: {e}"
                    logger.error(f"Cron job '{job.name}' agent_turn failed: {e}")
                    await _notify_error(err)
                    return f"__error__:{err}"

                delivery_error: str | None = None
                if response and not is_silent_response(response):
                    # Local surfaces (desktop / iOS / TUI reached over the
                    # gateway) have no outbound channel adapter, so
                    # publish_outbound silently drops the result — that's the
                    # "gateway cron never lands in chat" bug. Push those over the
                    # gateway WS instead (the same out-of-band path board results
                    # use). Relay ("web") and real channels (telegram/…) keep the
                    # adapter path below, so the working relay flow is untouched.
                    _local = origin_channel in ("cli", "tui", "desktop", "ios")
                    _gw = getattr(agent, "_gateway_server", None)
                    if _local and _gw is not None and hasattr(_gw, "push_session_message"):
                        _sk = f"{origin_channel}:{origin_chat_id}"
                        # Persist into the origin chat session so the result
                        # SURVIVES A RELOAD. The cron turn ran in its own
                        # "cron:{id}" session, so chat.history for the user's chat
                        # wouldn't otherwise contain it — the live push alone
                        # shows it until you leave and come back. (The relay path
                        # gets this for free via its Firestore write.)
                        try:
                            _sess = agent.sessions.get_or_create(_sk)
                            _sess.add_message("assistant", response)
                            agent.sessions.save(_sess)
                        except Exception as pe:
                            logger.warning(
                                f"Cron '{job.name}' history persist failed: {pe}"
                            )
                        try:
                            await _gw.push_session_message(_sk, response)
                        except Exception as de:
                            delivery_error = f"{type(de).__name__}: {de}"
                            logger.error(
                                f"Cron '{job.name}' gateway push failed: {delivery_error}"
                            )
                        # APNs/FCM via the relay too — the WS push only reaches a
                        # FOREGROUNDED app; this wakes a closed/backgrounded one.
                        # Fire-and-forget; no-op if no device registered push.
                        _schedule_cron_push_notification(job, response, conversation_id=_sk)
                    else:
                        from flowly.bus.events import OutboundMessage
                        try:
                            await bus.publish_outbound(OutboundMessage(
                                channel=channel,
                                chat_id=job.payload.to,
                                content=response,
                            ))
                        except Exception as de:
                            # Delivery failures are tracked separately from
                            # agent failures — the agent succeeded, the outbound
                            # transport is what's broken. Don't fail the run.
                            delivery_error = f"{type(de).__name__}: {de}"
                            logger.error(
                                f"Cron '{job.name}' delivery failed: {delivery_error}"
                            )
                elif is_silent_response(response):
                    logger.info(
                        f"Cron job '{job.name}': agent returned [SILENT] — "
                        f"skipping delivery"
                    )

                cron.mark_delivery_error(job.id, delivery_error)

                return response
            else:
                # No specific target. This is the Schedule-screen path for a
                # remote gateway: the run is archived under cron.output and
                # should not be injected into a chat, but a closed iOS app still
                # needs a banner when the job finishes.
                prompt = (
                    f"[Scheduled Task: {job.name}]\n"
                    f"{job.payload.message}\n\n"
                    "Return the result as plain text. It will be saved to the "
                    "scheduled task output view automatically; do not use the "
                    "message tool. If any tool fails or returns an error, report "
                    "it clearly in your final response."
                )

        try:
            fallback_origin_channel = (
                (job.origin.platform if job.origin else None)
                or job.payload.channel
            )
            fallback_origin_chat_id = (
                (job.origin.chat_id if job.origin else None)
                or job.payload.to
            )
            response = await agent.process_direct(
                _augment(prompt),
                session_key=f"cron:{job.id}",
                model_override=model_override,
                disabled_tools=CRON_DISABLED_TOOLS,
                skip_memory=True,
                skip_context_files=True,
                origin_channel=fallback_origin_channel,
                origin_chat_id=fallback_origin_chat_id,
            )
        except Exception as e:
            err = f"Agent execution failed: {e}"
            logger.error(f"Cron job '{job.name}' agent_turn failed: {e}")
            await _notify_error(err)
            return f"__error__:{err}"

        if job.payload.deliver and response and not is_silent_response(response):
            _schedule_cron_push_notification(job, response)

        return response

    cron.on_job = on_cron_job

    # Wire the inactivity watchdog so long-running cron jobs aren't
    # guillotined by a wall-clock timer. The poller reads the agent's
    # activity summary every 5s and only kills the run if NO progress
    # (no stream chunk, no tool call, no API response) has happened
    # for _JOB_TIMEOUT_S seconds — activity-driven inactivity kill.
    cron.activity_probe = agent.get_activity_summary
    cron.interrupt_fn = agent.interrupt

    # P1.1 — bridge subagent progress into parent's activity tracker.
    # Without this, a 10-min background subagent leaves the main agent's
    # `_last_activity_ts` frozen at the spawn moment, which the cron
    # inactivity poller reads as "idle" and interrupts the parent turn
    # before the child's result arrives.
    agent.subagents.parent_activity_touch = agent._touch_activity

    async def on_cron_alert(job: CronJob, alert_message: str) -> None:
        """Deliver a failure alert to the job's delivery target.

        Called by the service after `failure_alert_after` consecutive
        failures, subject to `failure_alert_cooldown_ms`. If the job has
        no delivery target configured, the alert is logged only.
        """
        channel = job.payload.channel or "telegram"
        chat_id = job.payload.to
        if job.payload.deliver and chat_id:
            from flowly.bus.events import OutboundMessage
            try:
                await bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=alert_message,
                ))
            except Exception as e:
                logger.warning(f"Cron alert delivery failed for '{job.name}': {e}")
        else:
            logger.warning(
                f"Cron alert for '{job.name}' not delivered "
                f"(no delivery target configured): {alert_message}"
            )

    cron.on_alert = on_cron_alert

    # Wire cron tool → web channel cronSessionId.
    # The relay provisions a stable cronSessionId on agent connect and sends
    # it in the `ready` message. Web-channel cron jobs must use this UUID as
    # `to` so they land in the "Scheduled Tasks" conversation (same behaviour
    # as desktop/web-created crons). Using a getter (not a cached value) so
    # the tool reads the latest value after reconnects.
    cron_tool_ref = agent.tools.get("cron")
    if cron_tool_ref and hasattr(cron_tool_ref, "set_web_cron_session_getter"):
        def _get_web_cron_session_id() -> str | None:
            web_ch = channels.get_channel("web")
            return getattr(web_ch, "cron_session_id", None) if web_ch else None
        cron_tool_ref.set_web_cron_session_getter(_get_web_cron_session_id)
        logger.info("[Gateway] Cron tool wired to web channel cronSessionId getter")

    # Firestore sync — bot-created tasks appear in iOS/web/desktop just like
    # user-created ones. Fire-and-forget via the relay WebSocket.
    if cron_tool_ref and hasattr(cron_tool_ref, "set_cron_sync_callbacks"):
        async def _on_cron_register(sync_payload: dict) -> None:
            web_ch = channels.get_channel("web")
            if web_ch and hasattr(web_ch, "send_cron_register"):
                try:
                    await web_ch.send_cron_register(sync_payload)
                except Exception as e:
                    logger.warning(f"[Gateway] cron.register relay push failed: {e}")

        async def _on_cron_unregister(name: str) -> None:
            web_ch = channels.get_channel("web")
            if web_ch and hasattr(web_ch, "send_cron_unregister"):
                try:
                    await web_ch.send_cron_unregister(name)
                except Exception as e:
                    logger.warning(f"[Gateway] cron.unregister relay push failed: {e}")

        cron_tool_ref.set_cron_sync_callbacks(on_register=_on_cron_register, on_unregister=_on_cron_unregister)
        logger.info("[Gateway] Cron tool wired to Firestore sync callbacks")

    # Reconciliation on every relay `ready` (handshake):
    #   - Fixes stale `to` fields on existing jobs (bot-created before this fix
    #     used random UUIDs that weren't registered in relay → delivery dropped).
    #   - Re-registers every web-delivery job with Firestore so tasks/{name}
    #     docs stay in sync after relay restarts or bot restarts.
    async def _reconcile_crons_on_ready() -> None:
        web_ch = channels.get_channel("web")
        if not web_ch:
            return
        current_sid = getattr(web_ch, "cron_session_id", None)
        if not current_sid:
            return
        try:
            jobs = cron.list_jobs(include_disabled=True)
            fixed = 0
            synced = 0
            for j in jobs:
                if not j.payload.deliver or j.payload.channel != "web":
                    continue

                # User-originated crons (the cron was created from a specific
                # chat — `to` points at the originating session/conversation)
                # must NOT be force-rewritten to cronSessionId. Doing so
                # reverts the chat-to-chat delivery fix and dumps every
                # cron reply into Scheduled Tasks again.
                #
                # Detection: the origin captured at creation matches the
                # stored delivery target. If they match we trust the user
                # intent and leave `to` alone; the relay is responsible
                # for keeping that sessionId routable (see Option B notes).
                is_user_originated = bool(
                    j.origin
                    and j.origin.chat_id
                    and j.payload.to
                    and j.payload.to == j.origin.chat_id
                )

                # 1. Repair stale `to` — only for jobs that SHOULD point at
                # Scheduled Tasks. User-originated jobs skip this branch.
                if not is_user_originated and j.payload.to != current_sid:
                    cron.update_delivery_target(j.id, "web", current_sid)
                    fixed += 1
                # 2. Re-sync to Firestore
                try:
                    await web_ch.send_cron_register({
                        "name": j.name,
                        "message": j.payload.message or "",
                        "schedule": {
                            "type": "interval" if j.schedule.kind == "every"
                            else "at" if j.schedule.kind == "at"
                            else "cron",
                            "intervalMs": j.schedule.every_ms,
                            "atMs": j.schedule.at_ms,
                            "expr": j.schedule.expr,
                        },
                        "channel": "web",
                    })
                    synced += 1
                except Exception as sync_err:
                    logger.warning(f"[Gateway] Reconcile sync failed for '{j.name}': {sync_err}")
            if fixed or synced:
                logger.info(f"[Gateway] Cron reconciled — repaired_to={fixed} synced_firestore={synced}")
        except Exception as e:
            logger.warning(f"[Gateway] Cron reconciliation failed: {e}")

    # The actual set_on_ready wiring is deferred until after ChannelManager is created.
    # See the block after `channels = ChannelManager(config, bus)` below.

    # Create heartbeat service — reads config from agents.defaults.heartbeat
    hb_cfg = config.agents.defaults.heartbeat

    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent with a fresh isolated session each tick."""
        # Clear previous heartbeat history so stale context doesn't bleed into this tick.
        hb_session = agent.sessions.get_or_create("heartbeat:tick")
        hb_session.clear()
        agent.sessions.save(hb_session)
        return await agent.process_direct(prompt, session_key="heartbeat:tick")

    active_hours_dict: dict[str, str] | None = None
    if hb_cfg.active_hours:
        active_hours_dict = {
            "start": hb_cfg.active_hours.start,
            "end": hb_cfg.active_hours.end,
            "timezone": hb_cfg.active_hours.timezone,
        }

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=hb_cfg.every_minutes * 60,
        enabled=hb_cfg.enabled,
        active_hours=active_hours_dict,
        deliver=hb_cfg.deliver,
    )

    # Create channel manager
    channels = ChannelManager(config, bus)

    # Wire cron reconciliation to web channel's on_ready — runs after every
    # handshake with the relay to fix stale `to` fields and re-sync tasks to
    # Firestore. Deferred to here because `channels` only exists now.
    _web_channel_ref = channels.get_channel("web")
    if _web_channel_ref and hasattr(_web_channel_ref, "set_on_ready"):
        _web_channel_ref.set_on_ready(_reconcile_crons_on_ready)
        logger.info("[Gateway] Cron reconciliation wired to web channel on_ready")

    # Set up compact/clear callbacks for channels + gateway
    async def on_compact(session_key: str, instructions: str | None = None) -> dict:
        """Handle /compact command from channels or desktop."""
        return await agent.compact_session(session_key, instructions)

    async def on_clear(session_key: str) -> dict:
        """Handle /clear command — clear session history."""
        session = agent.sessions.get_or_create(session_key)
        msg_count = len(session.messages)
        session.clear()
        agent.sessions.save(session)
        return {"success": True, "message": f"Cleared {msg_count} messages"}

    async def on_retry(session_key: str) -> dict:
        """Strip trailing assistant/tool chain and return last user text.

        The TUI then re-submits that text via ``chat.send`` to get a
        fresh assistant reply. Returns ``{ok, text, removed}`` so the
        client can surface a meaningful no-op message when there's
        nothing to retry (empty session, session ends on user).
        """
        session = agent.sessions.get_or_create(session_key)
        before = len(session.messages)
        text = session.drop_last_assistant_chain()
        if text is None:
            return {"ok": False, "text": "", "removed": 0,
                    "reason": "no user message to retry"}
        agent.sessions.save(session)
        removed = before - len(session.messages)
        return {"ok": True, "text": text, "removed": removed}

    async def on_undo(session_key: str) -> dict:
        """Remove the last user+assistant turn; return the popped user text.

        Caller can pre-fill the composer with the returned text so the
        user can edit and resubmit. Returns the same
        ``{ok, text, removed}`` shape as ``on_retry`` for symmetry.
        """
        session = agent.sessions.get_or_create(session_key)
        before = len(session.messages)
        text = session.drop_last_turn()
        if text is None:
            return {"ok": False, "text": "", "removed": 0,
                    "reason": "no user turn to undo"}
        agent.sessions.save(session)
        removed = before - len(session.messages)
        return {"ok": True, "text": text, "removed": removed}

    channels.set_compact_callback(on_compact)

    # Wire the Stop-button path. The web channel's ``chat.abort``
    # RPC fires this with the run_id of the turn to interrupt; the
    # agent's streaming loop polls ``is_run_aborted(run_id)``
    # between every chunk and breaks out cooperatively, preserving
    # the partial accumulated text so the user still sees what the
    # bot had managed to say. Without this wire-up the abort RPC
    # silently no-ops and the bot finishes its turn anyway.
    channels.set_abort_callback(agent.mark_aborted)

    # Legacy bridge fallback (disabled by default; integrated Python plugin is official path)
    legacy_voice_bridge_enabled = bool(config.integrations.voice.legacy_bridge_enabled)

    # Create gateway API callback for legacy voice bridge
    async def on_voice_message(call_sid: str, from_number: str, text: str) -> str:
        """Handle voice message from voice bridge."""
        # Use Telegram session if configured, otherwise use voice-specific session
        telegram_chat_id = config.integrations.voice.telegram_chat_id
        if telegram_chat_id:
            session_key = f"telegram:{telegram_chat_id}"
        else:
            session_key = f"voice:{call_sid}"

        # Format message with clear voice context
        prompt = f"""[ACTIVE PHONE CALL]
Call SID: {call_sid}
Caller: {from_number}

User said: "{text}"

IMPORTANT RULES:
1. This is a phone call — the user can only hear what you say.
2. Only use safe tools if needed (voice_call end/list, screenshot, message, system).
3. If you take a screenshot, it goes to Telegram automatically — tell the user "I sent the screenshot to Telegram".
4. To hang up: voice_call(action="end_call", call_sid="{call_sid}", message="Goodbye!")
5. Keep it short and clear — long sentences are hard to understand on the phone.

Respond to the user now:"""
        response = await agent.process_direct(prompt, session_key=session_key)
        return response or "Sorry, something went wrong. Could you say that again?"

    async def on_cron_run(job_id: str, force: bool) -> bool:
        return await cron.run_job(job_id, force=force)

    async def on_chat_message(
        session_key: str,
        message: str,
        run_id: str,
        stream_callback=None,
        media: list[str] | None = None,
        voice_mode: bool = False,
        iteration_callback=None,
    ) -> tuple[str, dict]:
        # Return ``(text, metadata)`` so the gateway can forward usage
        # tokens (prompt/completion/cache) to the TUI's context-window
        # indicator. Without metadata the bar would stay at 0/<budget>
        # forever even after long turns — the StatusBar only learns
        # token counts from the final chat event.
        text, metadata = await agent.process_direct(
            content=message,
            session_key=session_key,
            stream_callback=stream_callback,
            media=media,
            voice_mode=voice_mode,
            return_metadata=True,
            on_iteration=iteration_callback,
        )
        return text, (metadata or {})

    # Artifact store for gateway
    artifact_store = None
    if config.tools.artifact.enabled:
        from flowly.artifacts.store import get_store as get_artifact_store
        artifact_store = get_artifact_store(get_data_dir())

    # MCP write-plane control endpoint (Faz 3c): a localhost+token HTTP
    # API that `flowly mcp serve --allow-writes` calls to send messages and
    # resolve approvals. on_send parses 'channel:chat_id' and enqueues an
    # OutboundMessage on the same bus the channels dispatch from.
    import secrets as _secrets
    _mcp_control_token = _secrets.token_urlsafe(32)

    async def _mcp_control_send(target: str, message: str) -> bool:
        from flowly.bus.events import OutboundMessage
        if ":" not in target:
            return False
        channel, chat_id = target.split(":", 1)
        await bus.publish_outbound(OutboundMessage(
            channel=channel, chat_id=chat_id, content=message,
        ))
        return True

    # Hot-reload: re-read config from disk, re-resolve the active provider,
    # rebuild the OpenRouterProvider client, and swap it on the running
    # agent. Avoids the "I switched provider in the TUI but the gateway is
    # still using the old one" pitfall. Triggered by POST /api/provider/reload
    # from the integrations modal after Save.
    async def on_provider_reload() -> dict:
        from flowly.config.loader import load_config as _reload_cfg
        from flowly.integrations.active_provider import resolve_active_provider as _resolve
        try:
            fresh_cfg = _reload_cfg()
        except Exception as exc:
            return {"ok": False, "error": f"config read failed: {exc}"}
        new_active = _resolve(fresh_cfg)
        if new_active is None:
            return {"ok": False, "error": "no usable provider in current config"}
        fbk: list[str] = []
        if new_active.key != "flowly":
            pcfg = getattr(fresh_cfg.providers, new_active.key, None)
            if pcfg is not None:
                fbk = getattr(pcfg, "fallback_keys", []) or []
        # Build the new provider BEFORE swapping so a construction error
        # (e.g. OpenRouterProvider refusing an empty api_key) leaves the
        # old, working provider in place rather than wedging the agent.
        new_model = fresh_cfg.agents.defaults.model
        try:
            new_provider = build_provider(
                new_active,
                default_model=new_model,
                fallback_keys=fbk,
                config=fresh_cfg,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"provider build failed: {type(exc).__name__}: {exc}",
            }
        # Hot-swap BOTH the provider client AND the agent's selected
        # model. Previously we only swapped the provider, which left
        # ``agent.model`` pointing at the boot-time string — every chat
        # then went out with the OLD model id even after /model picked
        # a new one. The TUI saw "switched" but the response was from
        # the previous model.
        agent.provider = new_provider
        agent.model = new_model
        # Keep the compaction service in lockstep: it holds its own
        # provider/model refs (summarization + the flowly-proxy window
        # clamp both read them) — a hot-swap must not leave them stale.
        try:
            agent.compaction.provider = new_provider
            agent.compaction.model = new_model
        except Exception:  # noqa: BLE001
            pass
        # This reload is how the TUI/CLI applies an xAI Grok login — so
        # (de)register x_search to match the new credential state, otherwise
        # it wouldn't surface until a full gateway restart.
        try:
            agent.sync_xai_search_tool()
        except Exception as exc:
            logger.warning(f"[provider] x_search sync skipped: {exc}")
        logger.info(
            f"[provider] hot-reloaded → {new_active.source} · model={new_model}"
        )
        return {
            "ok": True,
            "key": new_active.key,
            "source": new_active.source,
            "api_base": new_active.api_base,
            "model": new_model,
        }

    # Let the shared feature_rpc model.set apply a model change LIVE (swap the
    # running provider + model) instead of restarting — same path the model
    # picker + /provider reload use.
    try:
        from flowly.channels import feature_rpc as _feature_rpc
        _feature_rpc.set_provider_reload_callback(on_provider_reload)
        # Board RPC (board.snapshot / board.action) over relay + gateway.
        _feature_rpc.set_board_provider(
            lambda: (getattr(agent, "_board_store", None), getattr(agent, "_board_orchestrator", None))
        )
        # Subagent registry — read-only, for board.card's run/tool-trace audit.
        _feature_rpc.set_registry_provider(
            lambda: getattr(getattr(agent, "subagents", None), "registry", None)
        )
        # Subagent manager — for subagents.spawn (manual background subagent).
        _feature_rpc.set_subagent_manager_provider(
            lambda: getattr(agent, "subagents", None)
        )
        # Cron RPC (cron.list / add / update / remove / run / output) over relay
        # + gateway. Same CronService the relay's Firestore/web path uses.
        _feature_rpc.set_cron_provider(lambda: cron)
    except Exception:
        pass

    gateway_server = GatewayServer(
        host=effective_host,
        port=port,
        auth_token=auth_token,
        on_voice_message=on_voice_message if legacy_voice_bridge_enabled else None,
        on_cron_run=on_cron_run,
        on_cron_reload=cron.reload,
        on_cron_health=cron.health_report,
        on_chat_message=on_chat_message,
        sessions=agent.sessions,
        subagent_registry=agent._subagent_registry,
        artifact_store=artifact_store,
        board_store=getattr(agent, "_board_store", None),
        board_orchestrator=getattr(agent, "_board_orchestrator", None),
        on_compact=on_compact,
        on_clear=on_clear,
        on_retry=on_retry,
        on_undo=on_undo,
        on_provider_reload=on_provider_reload,
        on_send=_mcp_control_send,
        control_token=_mcp_control_token,
    )

    # Wire browser_tab tool to gateway server
    agent.set_gateway_server(gateway_server)

    # Mirror relay/web chat liveness to local desktop gateway clients. This
    # lets the desktop UI react when another client (iOS/web) talks to this
    # machine's bot without forcing the desktop to join that relay chat.
    web = channels.get_channel("web")
    if web and hasattr(web, "set_local_event_callback"):
        web.set_local_event_callback(gateway_server.broadcast_event)

    # Wire artifact broadcast callback — push to desktop (gateway) AND relay (web channel)
    if artifact_store:
        artifact_tool = agent.tools.get("artifact")
        if artifact_tool:
            async def _broadcast_artifact(event_name: str, data: dict) -> None:
                # Desktop clients (direct WS)
                await gateway_server._broadcast_artifact_event(event_name, data)
                # Relay (web channel) — so relay can sync to S3 + Firestore
                web = channels.get_channel("web")
                if web and hasattr(web, "_ws") and web._ws:
                    import json as _json
                    try:
                        await web._ws.send(_json.dumps({
                            "type": "event",
                            "event": event_name,
                            "data": data,
                        }))
                    except Exception:
                        pass  # Non-critical — relay sync is best-effort
            artifact_tool.set_on_change(_broadcast_artifact)
            # Share with SubagentManager so subagent artifacts also sync to S3
            agent.subagents._artifact_on_change = _broadcast_artifact

    # Wire flowlet broadcast + agent-action runner — same desktop(gateway)+relay
    # fan-out as artifacts. The broadcast callback also backs feature_rpc's
    # flowlets.action / flowlets.delete so a tap on one client updates the rest.
    async def _broadcast_flowlet(event_name: str, data: dict) -> None:
        await gateway_server.broadcast_event(event_name, data)
        _web = channels.get_channel("web")
        if _web and hasattr(_web, "_ws") and _web._ws:
            import json as _json
            try:
                await _web._ws.send(_json.dumps({
                    "type": "event", "event": event_name, "data": data,
                }))
            except Exception:
                pass  # relay sync is best-effort

    async def _flowlet_agent_runner(flowlet: dict, message: str) -> None:
        """Run an agent turn for a flowlet `agent` action and deliver the reply
        back to the screen's origin chat. Best-effort: never raises into the
        action path."""
        origin = flowlet.get("origin_session") or ""
        channel, _, chat_id = origin.partition(":")
        name = flowlet.get("name") or "flowlet"
        prompt = (
            f"[Flowlet action — {name}]\n"
            f"The user tapped an action on their '{name}' mini-screen. "
            f"Their request:\n{message}\n\n"
            "Reply in plain text; it is delivered directly to the user. "
            "You can read the screen's live data with the flowlet tool "
            "(action=get). Keep it short and useful."
        )
        session_key = origin if origin else f"flowlet:{flowlet.get('id')}"
        try:
            response = await agent.process_direct(
                prompt,
                session_key=session_key,
                origin_channel=channel or None,
                origin_chat_id=chat_id or None,
                skip_memory=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Flowlet agent action failed: {}", exc)
            return
        if not response or is_silent_response(response):
            return
        _local = channel in ("cli", "tui", "desktop", "ios")
        if _local and hasattr(gateway_server, "push_session_message"):
            try:
                await gateway_server.push_session_message(session_key, response)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flowlet push failed: {}", exc)
        elif chat_id:
            from flowly.bus.events import OutboundMessage
            try:
                await bus.publish_outbound(OutboundMessage(
                    channel=channel or "telegram", chat_id=chat_id, content=response,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flowlet outbound failed: {}", exc)

    try:
        from flowly.channels import feature_rpc as _frpc_flowlet
        _frpc_flowlet.set_flowlet_broadcast(_broadcast_flowlet)
        _frpc_flowlet.set_flowlet_agent_runner(_flowlet_agent_runner)
        _flowlet_tool = agent.tools.get("flowlet")
        if _flowlet_tool and hasattr(_flowlet_tool, "set_on_change"):
            _flowlet_tool.set_on_change(_broadcast_flowlet)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Flowlet wiring skipped: {}", exc)

    # Wire auto-compaction notification — push to relay (web channel) + desktop (gateway)
    async def _on_auto_compaction(
        session_key: str, tokens_before: int, tokens_after: int, messages_removed: int,
        phase: str = "completed",
    ) -> None:
        data = {
            "phase": phase,
            "tokensBefore": tokens_before,
            "tokensAfter": tokens_after,
            "messagesRemoved": messages_removed,
            "sessionKey": session_key,
        }
        # Relay (web channel)
        web = channels.get_channel("web")
        if web and hasattr(web, "send_compaction_event"):
            await web.send_compaction_event(session_key, tokens_before, tokens_after, messages_removed, phase)
        # Desktop clients (direct WS)
        await gateway_server._broadcast_compaction_event(data)

    agent._on_compaction = _on_auto_compaction

    # Wire auto-title → relay (encrypt + persist on the conversation doc).
    # Gateway sessions have no relay mapping, so send_title_event no-ops for
    # them — they already surface the title via sessions.list.
    async def _on_session_titled(session_key: str, title: str) -> None:
        web = channels.get_channel("web")
        if web and hasattr(web, "send_title_event"):
            await web.send_title_event(session_key, title)

    agent._on_session_titled = _on_session_titled

    # Wire subagent events → gateway broadcast
    async def _on_subagent_event(event_name: str, data: dict) -> None:
        await gateway_server._broadcast_subagent_event(event_name, data)

    agent.subagents._on_event = _on_subagent_event

    # Board completion delivery is handled inside the agent loop: the
    # orchestrator wakes the agent with the result (relay turn) and the
    # agent's reply reaches local clients via _process_system_message's
    # gateway push, or remote channels via their adapter. No wiring needed
    # here beyond the gateway reference already set by set_gateway_server().

    # Wire tool lifecycle events (tool.start / tool.complete) → WS broadcast.
    # Lightweight: emitted from agent loop around tools.execute(); failure
    # in the callback never affects agent execution.
    async def _on_tool_event(event_name: str, data: dict) -> None:
        await gateway_server.broadcast_tool_event(event_name, data)

    agent.tool_callback = _on_tool_event

    # Wire cron completion (cron.completed) → WS broadcast so desktop clients
    # can raise a native OS notification when a scheduled job finishes. The
    # cron service was created before the gateway server existed (it's needed
    # by the agent), so the callback is attached here once both are live.
    async def _on_cron_complete(event_name: str, data: dict) -> None:
        await gateway_server.broadcast_cron_event(event_name, data)

    cron.on_complete = _on_cron_complete

    # Wire delegate tool to same subagent event callback (if multi-agent is active)
    delegate = agent.tools.get('delegate_to')
    if delegate and hasattr(delegate, '_on_event'):
        delegate._on_event = _on_subagent_event
        gateway_server._delegate_tool = delegate
        gateway_server._subagent_manager = agent.subagents
        logger.info("[Gateway] Delegate tool wired to subagent event broadcast")
    else:
        logger.info(f"[Gateway] Delegate tool not wired: found={delegate is not None}, has_attr={hasattr(delegate, '_on_event') if delegate else 'N/A'}")

    # P2.8 — expose the assistant registry so the desktop UI's "Your
    # agents" section can list / reload user-defined assistants.
    gateway_server._assistant_registry = getattr(agent, "_assistant_registry", None)

    # Wire Meeting Coach (continuous listening + real-time coaching).
    # STT runs client-side (desktop → web-app /api/stt/transcribe); the
    # gateway only receives transcribed text, so we just need an LLM.
    try:
        from flowly.coaching import CoachingManager
        from flowly.memory.knowledge_graph import KnowledgeGraph

        coaching_kg = None
        try:
            state_dir = getattr(agent, "_state_dir", None) or Path(get_flowly_home())
            kg_path = Path(state_dir) / "knowledge_graph.sqlite3"
            kg_path.parent.mkdir(parents=True, exist_ok=True)
            coaching_kg = KnowledgeGraph(str(kg_path))
        except Exception as e:
            logger.warning(f"[Gateway] Meeting Coach KG unavailable: {e}")

        workspace = getattr(agent, "workspace", None) or Path(get_flowly_home())
        memory_path = Path(workspace) / "memory" / "MEMORY.md"

        coaching_mgr = CoachingManager(
            llm_provider=agent.provider,
            knowledge_graph=coaching_kg,
            memory_path=memory_path,
            artifact_store=getattr(agent, "_artifact_store", None) or gateway_server.artifact_store,
        )
        gateway_server._coaching_manager = coaching_mgr
        console.print("[green]✓[/green] Meeting Coach: enabled")
    except Exception as e:
        logger.warning(f"[Gateway] Meeting Coach wiring failed: {e}")

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every 30m")
    if legacy_voice_bridge_enabled:
        console.print("[yellow]⚠[/yellow] Legacy voice bridge fallback enabled")

    # Initialize voice plugin if enabled
    voice_plugin = None
    if config.integrations.voice.enabled:
        voice_cfg = config.integrations.voice
        if voice_cfg.twilio_account_sid and voice_cfg.twilio_auth_token:
            try:
                from flowly.voice.plugin import VoicePlugin
                voice_plugin = VoicePlugin(config, agent)
                # Connect voice plugin to agent's voice tool
                agent.set_voice_plugin(voice_plugin)
                console.print(f"[green]✓[/green] Voice calls: initializing...")
            except Exception as e:
                console.print(f"[yellow]Warning: Voice plugin failed to initialize: {e}[/yellow]")
        else:
            console.print(f"[yellow]Warning: Voice enabled but Twilio credentials not configured[/yellow]")

    console.print(f"[green]✓[/green] API: http://{config.gateway.host}:{port}")

    async def run():
        shutdown_event = asyncio.Event()

        def signal_handler():
            console.print("\n[yellow]Shutting down...[/yellow]")
            shutdown_event.set()

        if platform.system() == "Windows":
            # Windows asyncio doesn't support loop.add_signal_handler
            signal.signal(signal.SIGINT, lambda s, f: signal_handler())
            signal.signal(signal.SIGTERM, lambda s, f: signal_handler())
        else:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, signal_handler)

        try:
            await gateway_server.start()
            await cron.start()
            await heartbeat.start(run_on_start=True)

            # Start voice plugin if available
            if voice_plugin:
                await voice_plugin.start(host="0.0.0.0", port=8765)
                if voice_plugin._ngrok_tunnel:
                    console.print(f"[green]✓[/green] Voice calls: ngrok tunnel ({voice_plugin._webhook_base_url})")
                else:
                    console.print(f"[green]✓[/green] Voice calls: integrated ({voice_plugin._webhook_base_url})")

            # Connect exec approval manager to channels and gateway
            from flowly.exec.approval_manager import get_approval_manager
            _approval_mgr = get_approval_manager()

            async def _notify_approval(pending) -> None:
                """Notify all channels about a pending exec approval."""
                import time as _time
                timeout_s = max(1, int(pending.expires_at - _time.time()))

                # Telegram: send inline buttons
                tg = channels.get_channel("telegram")
                if tg and hasattr(tg, "send_approval_prompt"):
                    # Determine which chat_id to send to
                    session_key = pending.session_key or ""
                    if session_key.startswith("telegram:"):
                        try:
                            chat_id = int(session_key.split(":", 1)[1])
                            await tg.send_approval_prompt(chat_id, pending.id, pending.request.command, timeout_s, getattr(pending, "supports_always", True))
                        except (ValueError, IndexError):
                            pass

                # iMessage: plain-text notice — iMessage has no inline
                # buttons, so the approve/deny itself happens in the
                # desktop app / TUI (broadcast below reaches those).
                im = channels.get_channel("imessage")
                if im:
                    session_key = pending.session_key or ""
                    if session_key.startswith("imessage:"):
                        from flowly.bus.events import OutboundMessage as _IMOutbound
                        await im.send(_IMOutbound(
                            channel="imessage",
                            chat_id=session_key.split(":", 1)[1],
                            content=(
                                "🔒 Command approval required:\n"
                                f"{pending.request.command}\n\n"
                                f"Approve or deny from the Flowly app (expires in {timeout_s}s)."
                            ),
                        ))

                # Web channel (relay): push to iOS/browser clients
                web = channels.get_channel("web")
                if web and hasattr(web, "send_approval_event"):
                    session_key = pending.session_key or ""
                    if session_key.startswith("web:"):
                        await web.send_approval_event(
                            session_key, pending.id, pending.request.command, pending.expires_at,
                            getattr(pending, "supports_always", True),
                        )

                # Gateway: push event to all desktop clients (local WS)
                await gateway_server.broadcast_approval_request(
                    pending.id, pending.request.command, pending.session_key, pending.expires_at,
                    getattr(pending, "supports_always", True),
                )

                # Closed-app push: wake the phone with an APNs/FCM notification
                # so the request reaches the user even when the app is shut.
                # Same relay path the board uses; tapping opens the app where
                # the live event above drives approve/deny.
                from flowly.push.approval_push import notify_approval_requested
                await notify_approval_requested(pending)

            _approval_mgr.add_notify_callback(_notify_approval)

            # Connect clarify manager to the gateway so agent-initiated
            # questions reach desktop/web clients. Same fan-out shape as
            # approvals; channels (Telegram, etc.) can be added later.
            from flowly.clarify.manager import get_clarify_manager
            _clarify_mgr = get_clarify_manager()

            async def _notify_clarify(pending) -> None:
                """Notify surfaces about a pending clarify question."""
                # Web channel (relay): push to iOS/browser/cloud-connected
                # desktop clients.
                web = channels.get_channel("web")
                if web and hasattr(web, "send_clarify_event"):
                    session_key = pending.session_key or ""
                    if session_key.startswith("web:"):
                        await web.send_clarify_event(
                            session_key, pending.id, pending.question,
                            pending.choices, pending.expires_at,
                        )

                # Gateway: push event to all desktop clients (local WS).
                await gateway_server.broadcast_clarify_request(
                    pending.id,
                    pending.question,
                    pending.choices,
                    pending.session_key,
                    pending.expires_at,
                )

            _clarify_mgr.add_notify_callback(_notify_clarify)

            # Run until shutdown signal
            async def run_until_shutdown():
                await asyncio.gather(
                    agent.run(),
                    channels.start_all(),
                )

            # Create main task
            main_task = asyncio.create_task(run_until_shutdown())

            # Wait for either shutdown signal or task completion
            done, pending = await asyncio.wait(
                [main_task, asyncio.create_task(shutdown_event.wait())],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        finally:
            # Graceful shutdown
            console.print("[dim]Cleaning up...[/dim]")
            if voice_plugin:
                await voice_plugin.stop()
            await gateway_server.stop()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()
            console.print("[green]✓[/green] Shutdown complete")

    asyncio.run(run())
