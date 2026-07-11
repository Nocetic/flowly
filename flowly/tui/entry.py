"""TUI launcher (``run_tui``).

The TUI is what bare ``flowly`` opens once a provider is configured (the
session/theme/gateway flags live on the root ``flowly`` command — there is
no separate ``flowly tui`` subcommand). Internal callers use ``run_tui``
directly.

TUI is always wired to the **local gateway**. There is no in-TUI cloud-sync
toggle: a CLI user typically doesn't want their terminal sessions backed up
to Firestore, and forcing the relay path here made connection state confusing
("am I on local? am I on relay? why doesn't my message appear in the desktop
app?"). What you sign in for via ``/login`` is **iOS pairing** — that wires
the local gateway's ``channels.web`` config so the relay can reach this
machine. The TUI itself stays on the local socket either way.
"""

from __future__ import annotations

import socket
import time

import typer
from rich.console import Console

from flowly.tui.state import (
    canonical_session_key,
    fresh_session_key,
    load_state,
    save_state,
)

console = Console()


def _gateway_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_startup_session(session: str, resume: bool) -> str:
    """Pick the session key for a TUI launch.

    Order:
      1. ``--session`` explicit key (canonicalised).
      2. ``--resume`` → ``tui_state.json`` ``last_session_key``
         (canonicalised on read; legacy ``tui-…`` keys keep resolving to
         their original on-disk JSONL — see the note below).
      3. Default: a brand-new session on every launch. Earlier chats stay
         one Ctrl+S (sessions picker) or ``--resume`` away.

    Legacy state files may hold ``cli:tui-…`` or raw ``tui-…`` keys written
    by older builds. We don't rewrite them — promoting an old key to the new
    format would orphan its on-disk JSONL. The canonicalisation step only
    adds the ``cli:`` prefix when missing, so a stored
    ``cli:tui-20260528-043250`` resumes against the same
    ``cli_tui-20260528-043250.jsonl`` it always did.
    """
    if session:
        return canonical_session_key(session)
    if resume:
        state = load_state()
        raw_key = state.get("last_session_key") or ""
        canonical = canonical_session_key(raw_key) if raw_key else ""
        if canonical and canonical != raw_key:
            state["last_session_key"] = canonical
            save_state(state)
        return canonical or fresh_session_key()
    return fresh_session_key()


def _format_age(modified_ms: int) -> str:
    delta = max(0.0, time.time() - modified_ms / 1000)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _resume_rows(
    sessions: list[dict], *, limit: int = 25
) -> list[tuple[str, str]]:
    """(session key, display label) pairs for the ``--resume`` picker."""
    rows: list[tuple[str, str]] = []
    for item in sessions[:limit]:
        key = str(item.get("key") or "")
        if not key:
            continue
        title = " ".join(
            str(item.get("title") or item.get("displayName") or key).split()
        )
        if len(title) > 58:
            title = title[:57] + "…"
        channel = str(item.get("channel") or "")
        prefix = "" if channel in ("cli", "") else f"[{channel}] "
        marker = "● " if item.get("running") else ""
        age = _format_age(int(item.get("modifiedAt") or 0))
        rows.append((key, f"{marker}{prefix}{title}  ·  {age}"))
    return rows


def _prompt_pick(rows: list[tuple[str, str]]) -> str | None:
    """Arrow-key session menu (same InquirerPy look as ``flowly setup``).

    Returns the picked session key, or ``None`` when the user backs out
    with Esc / Ctrl-C.
    """
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    try:
        return inquirer.select(
            message="Resume a session",
            choices=[Choice(value=key, name=label) for key, label in rows],
            pointer="›",
            mandatory=False,
            height="100%",
            keybindings={"skip": [{"key": "escape"}]},
        ).execute()
    except KeyboardInterrupt:
        return None


def pick_resume_session() -> str | None:
    """Standalone ``--resume`` menu shown before the TUI launches.

    Lists recent sessions newest-first; the selection becomes the TUI's
    session key. Returns ``None`` when the user backs out (caller exits).
    No sessions yet → fall through to a fresh key with a note.
    """
    from flowly.channels.feature_rpc import sessions_list

    try:
        sessions = sessions_list().get("sessions", [])
    except Exception:
        sessions = []
    if not sessions:
        console.print("[dim]No saved sessions yet — starting a fresh one.[/dim]")
        return fresh_session_key()
    rows = _resume_rows(sessions)
    try:
        picked = _prompt_pick(rows)
    except Exception:
        # Non-interactive terminal (prompt_toolkit can't render): behave
        # like a plain resume and reopen the most recent session.
        return resolve_startup_session("", True)
    if picked is None:
        return None
    return canonical_session_key(picked)


def run_tui(
    *,
    host: str = "127.0.0.1",
    port: int = 18790,
    session: str = "",
    new: bool = False,
    resume: bool = False,
    theme: str = "",
    open_modal: str = "",
) -> None:
    """Plain-Python TUI launcher.

    Exists so internal callers (the smart-entry callback in
    ``commands.py``, ``flowly setup``'s redirect into the integrations
    modal) can launch the TUI without paying for Typer's ``OptionInfo``
    defaults — those only resolve when Typer invokes the CLI command,
    so calling :func:`tui` directly from Python would pass
    ``OptionInfo`` instances everywhere and crash on the first usage.
    """
    if theme:
        from flowly.tui.theme import get_theme, list_themes
        if get_theme(theme) is None:
            available = ", ".join(t.name for t in list_themes())
            console.print(
                f"[red]Unknown TUI theme:[/red] {theme}\n"
                f"Available themes: [bold]{available}[/bold]"
            )
            raise typer.Exit(code=2)

    if not _gateway_reachable(host, port):
        # Distinguish "no provider yet" (→ run setup) from "provider ready
        # but gateway not running" (→ start it). A fresh user hits the
        # former; pointing them at `flowly gateway` there would just bounce
        # them off the gateway's own no-provider error.
        configured = False
        try:
            from flowly.config.loader import load_config
            from flowly.integrations.active_provider import (
                resolve_active_provider,
            )
            configured = resolve_active_provider(load_config()) is not None
        except Exception:
            configured = False

        if not configured:
            # Fresh install — run the unified onboarding (account or API key)
            # instead of a bare "not configured" message. TTY-guarded inside;
            # non-interactive contexts just get guidance and return.
            from flowly.cli.onboard_cmd import run_onboarding
            run_onboarding()
            raise typer.Exit(code=0)

        console.print(f"[red]Gateway not reachable on {host}:{port}.[/red]")
        console.print("Start the gateway, then run [bold]flowly[/bold] again:")
        console.print(
            "  [bold]flowly service install --start[/bold]  "
            "[dim](background)[/dim]"
        )
        console.print(
            "  [bold]flowly gateway[/bold]                  "
            "[dim](foreground, in another terminal)[/dim]"
        )
        raise typer.Exit(code=1)

    # Every launch starts a fresh session by default. ``--resume`` opens a
    # terminal session picker (like ``flowly setup``'s menus) and launches
    # the TUI on the choice; ``--session`` targets an explicit key. ``new``
    # is kept for backward compatibility — it now names the default.
    if resume and not session:
        picked = pick_resume_session()
        if picked is None:
            raise typer.Exit(code=0)
        session_key = picked
    else:
        session_key = resolve_startup_session(session, resume=False)

    try:
        from flowly.tui.app import FlowlyTUI
    except ImportError as exc:
        console.print(
            f"[red]Textual not installed:[/red] {exc}\n"
            f"Run: [bold]pip install textual[/bold]"
        )
        raise typer.Exit(code=1) from exc

    model_hint = ""
    gateway_token = ""
    try:
        from flowly.config.loader import load_config
        cfg = load_config()
        model_hint = cfg.agents.defaults.model or ""
        # When the gateway is exposed remotely it requires auth for every
        # client — including this same-machine TUI. Present the configured
        # token so loopback stays usable (empty token → no-op, unchanged).
        gateway_token = (cfg.gateway.token or "").strip()
    except Exception:
        pass

    from flowly.tui.client import GatewayClient
    client = GatewayClient(host=host, port=port, token=gateway_token)

    app = FlowlyTUI(
        host=host,
        port=port,
        session_key=session_key,
        model_hint=model_hint,
        client=client,
        auto_open_modal=open_modal or None,
        theme_name=theme or None,
    )
    app.run()
