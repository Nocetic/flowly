"""``flowly login`` — headless device-code OAuth + relay repair.

Stand-alone CLI mirror of the TUI's ``/login`` slash command. Three
distinct operations live behind one command:

  ``flowly login``
      Full OAuth flow for a brand-new install. Opens a browser, polls
      until the user approves, then registers the machine and wires
      relay credentials. No-ops with an "already signed in" message
      when tokens are present AND the relay config is healthy.

  ``flowly login`` *(account exists but relay config is incomplete)*
      Detection-only — surfaces the gap with the exact command to
      run. **Never** silently mutates state on a bare ``login`` call;
      that would surprise audit trails.

  ``flowly login --repair``
      Re-registers the machine and re-writes the relay config using
      the existing keychain tokens. No browser, no OAuth. Idempotent
      on a healthy install. Fails cleanly with exit code 1 when the
      refresh token is unusable (forces the user to run a full
      ``flowly login`` to recover).

  ``flowly login --repair --dry-run``
      Prints the changes that would be applied without touching
      config / keychain / backend.

Exit codes
----------
    0   success (or "already healthy" no-op)
    1   token refresh failed — user must run full re-login
    2   backend / network error during register_machine
    3   ``--repair`` invoked but nothing to repair
    130 Ctrl-C / EOF
"""

from __future__ import annotations

import asyncio
import sys
import webbrowser
from dataclasses import dataclass, field
from typing import Optional

import typer
from rich.console import Console

from flowly.account.health import (
    check_active_provider,
    check_provider_corruption,
    check_relay_state,
)

console = Console()


# State helpers live in ``flowly.account.health`` so ``flowly doctor``
# and ``flowly login`` share the same definition of "healthy". Local
# aliases below keep this file readable without exposing the import
# chain everywhere.
_check_relay_state = check_relay_state
_check_provider_state = check_active_provider


# ── Repair application ──────────────────────────────────────────────


@dataclass
class _RepairResult:
    relay_wired: bool = False
    relay_changed: bool = False         # vs. previous on-disk state
    server_id: str = ""
    server_name: str = ""
    server_existing: bool = False
    provider_promoted: bool = False
    provider_promoted_to: str = ""
    notes: list[str] = field(default_factory=list)


async def _apply_repair(account, *, dry_run: bool) -> _RepairResult:
    """Re-register server + wire relay + (conditionally) set default provider.

    The single mutation pathway used by both the full-login finale
    and ``--repair``. Idempotent against an already-healthy state.
    Refuses to clobber an existing non-flowly provider — matches the
    same guard the TUI login modal enforces.
    """
    from flowly.account import audit_log
    from flowly.account.relay_config import wire_relay_credentials
    from flowly.account.server import register_machine

    result = _RepairResult()

    if dry_run:
        # Don't talk to the backend or touch disk — just enumerate
        # the deltas the live path would attempt.
        relay = _check_relay_state()
        is_active, slug = _check_provider_state()
        plan: list[str] = []
        plan.append(
            "would POST /api/servers (idempotent on machineId)"
        )
        if not relay.healthy:
            plan.append(f"would re-wire channels.web ({relay.reason})")
        else:
            plan.append("relay config already healthy — no-op")
        if not is_active:
            plan.append("would set providers.active = 'flowly' (was empty)")
        else:
            plan.append(f"would NOT change providers.active (currently '{slug}')")
        for line in plan:
            console.print(f"  [dim]· {line}[/]")
        audit_log.info(
            "cli.login.repair_dry_run",
            relay_healthy=relay.healthy,
            relay_reason=relay.reason,
            provider_active=slug,
        )
        return result

    # 1. Register-or-reuse the machine in Firestore.
    srv = await register_machine(account.id_token)
    account.server_id = srv.server_id
    account.server_name = srv.name
    account.gateway_auth_token = srv.gateway_auth_token

    from flowly.account.auth import save_account
    save_account(account)

    # 2. Wire relay credentials into ``channels.web``.
    change = wire_relay_credentials(srv)
    result.relay_wired = True
    result.relay_changed = change.changed
    result.server_id = srv.server_id
    result.server_name = srv.name
    result.server_existing = srv.existing

    # 3. Auto-promote Flowly to default provider ONLY when nothing is
    #    set. Same guard as ``flowly/tui/panes/login_modal.py`` so a
    #    BYOK user who happens to repair their relay config doesn't
    #    lose their LLM choice.
    is_active, slug = _check_provider_state()
    if not is_active:
        try:
            from flowly.integrations.active_provider import set_active_provider
            set_active_provider("flowly")
            result.provider_promoted = True
            result.provider_promoted_to = "flowly"
        except Exception as exc:  # noqa: BLE001
            result.notes.append(f"provider auto-set failed: {exc}")

    audit_log.info(
        "cli.login.repair_applied",
        server_id=srv.server_id,
        server_existing=srv.existing,
        relay_changed=change.changed,
        provider_promoted=result.provider_promoted,
        previous_active_provider=slug,
    )
    return result


def _print_repair_summary(result: _RepairResult, *, header: str) -> None:
    console.print()
    console.print(f"  [green]✓[/] {header}")
    if result.relay_wired:
        if result.relay_changed:
            verb = "registered" if not result.server_existing else "reused"
            console.print(
                f"  [green]✓[/] Server {verb} → "
                f"[cyan]{result.server_name}[/]"
            )
            console.print(
                "  [green]✓[/] Relay config wired → "
                "[dim]wss://relay.useflowlyapp.com/relay[/]"
            )
        else:
            console.print(
                f"  [dim]·[/] Relay config already healthy "
                f"([cyan]{result.server_name}[/]) — no change"
            )
    if result.provider_promoted:
        console.print(
            f"  [green]✓[/] Provider set → [b]{result.provider_promoted_to}[/] "
            "(hosted, no API key required)"
        )
    for note in result.notes:
        console.print(f"  [yellow]⚠[/] {note}")


# ── Command ─────────────────────────────────────────────────────────


def _login_with_account_key(key: str) -> None:
    """Connect to Flowly with an account key (``flw_…``) — the no-browser path
    for self-hosted / CLI bots NOT managed by the Desktop app.

    Writes the key as the flowly provider credential and makes flowly the active
    provider. Billed to the user's account. Does NOT register a server or touch
    relay (``channels.web``), so the bot stays exactly as it is.
    """
    from flowly.config.loader import load_config, save_config

    key = key.strip()
    if not key.startswith("flw_"):
        console.print(
            "[red]✗ That doesn't look like a Flowly account key.[/] "
            "Expected an [cyan]flw_…[/] value (create one in the Flowly dashboard)."
        )
        raise typer.Exit(code=2)

    cfg = load_config()
    cfg.providers.flowly.account_key = key
    cfg.providers.flowly.enabled = True
    save_config(cfg)
    # Explicit intent ("use flowly with this key") → switch active. Routed
    # through set_active_provider so the default model is auto-fixed when the
    # flowly proxy can't serve the current one.
    from flowly.integrations.active_provider import set_active_provider
    model_changed = set_active_provider("flowly")
    try:
        from flowly.integrations import model_catalog
        model_catalog.flush_cache()
    except Exception:  # noqa: BLE001
        pass

    console.print(
        "[green]✓[/] Flowly account key saved — provider set to [cyan]flowly[/]."
        + (f" Model → [b]{model_changed}[/b]." if model_changed else "")
    )
    console.print(
        "  LLM usage is billed to your Flowly account. "
        "Run [cyan]flowly[/] to start chatting."
    )


def _mint_and_save_account_key(account) -> bool:
    """Auto-provision the account-key provider after sign-in (shared with the TUI
    login modal). Idempotent + best-effort — see ``flowly.account.account_key``."""
    from flowly.account.account_key import ensure_account_key
    return ensure_account_key(account)


def login(
    no_browser: bool = typer.Option(
        False, "--no-browser",
        help="Don't try to open the authorization URL in a browser",
    ),
    repair: bool = typer.Option(
        False, "--repair",
        help="Re-register + re-wire relay config using existing tokens (no browser).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what `--repair` would change without writing anything.",
    ),
    key: str = typer.Option(
        "", "--key",
        help="Use a Flowly account key (flw_…) you already have (e.g. from the Desktop "
             "app) — sets the flowly provider with no server record and no relay.",
    ),
    relay_opt: Optional[bool] = typer.Option(
        None, "--relay/--no-relay",
        help="Skip the interactive prompt and force remote/phone reach (a server "
             "registration + relay) ON or OFF. Default: ask interactively.",
    ),
) -> None:
    """Sign in with Flowly account — zero API keys, OAuth-driven.

    Without flags: full OAuth flow for a fresh machine, or a noop
    "already signed in" message when tokens AND relay config are
    healthy. If tokens exist but relay config is broken (someone edited
    config.json, partial restore, etc.), the command points you at
    ``flowly login --repair``.

    With ``--repair``: re-registers the machine and re-writes the
    relay config without launching a browser. Uses the keychain
    tokens already on disk. Exits with code 1 if those tokens can't
    be refreshed — run plain ``flowly login`` to recover.
    """
    from flowly.account import audit_log
    from flowly.account.auth import (
        FirebaseAuthError,
        LoginTimeout,
        credential_storage_status,
        load_account_refreshing,
        load_account_sync,
        run_login_flow,
    )

    # ── Path: --key (use a key you already have — no browser, no relay) ──
    # Programmatic / Desktop path: a key was provided, just store it.
    if key:
        _login_with_account_key(key)
        return

    if dry_run and not repair:
        console.print(
            "[red]--dry-run only applies with --repair.[/]\n"
            "  Use: [cyan]flowly login --repair --dry-run[/]"
        )
        raise typer.Exit(code=2)

    existing = load_account_sync()

    # ── Path: --repair (explicit, uses existing tokens) ──────────
    if repair:
        if existing is None:
            console.print(
                "[red]✗ Nothing to repair — not signed in.[/]\n"
                "  Run [cyan]flowly login[/] for a full sign-in flow."
            )
            audit_log.info("cli.login.repair_no_account")
            raise typer.Exit(code=3)

        # Refresh the id_token before talking to the backend — repair
        # is useless with an expired token.
        try:
            account = asyncio.run(load_account_refreshing())
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[red]✗ Token refresh failed:[/] {exc}\n"
                "  Run [cyan]flowly login[/] for a full re-login."
            )
            audit_log.error("cli.login.repair_refresh_failed", error=str(exc))
            raise typer.Exit(code=1)
        if account is None:
            console.print(
                "[red]✗ Refresh token rejected.[/] "
                "Run [cyan]flowly login[/] for a full re-login."
            )
            audit_log.info("cli.login.repair_token_unusable")
            raise typer.Exit(code=1)

        # Short-circuit if everything is already wired AND not dry-run —
        # don't burn a backend call for nothing.
        if not dry_run:
            relay = _check_relay_state()
            is_active, _ = _check_provider_state()
            if relay.healthy and is_active:
                console.print(
                    "[green]✓[/] Nothing to repair — relay config and "
                    "provider are already healthy."
                )
                audit_log.info("cli.login.repair_skipped_healthy",
                               server_id=relay.server_id)
                raise typer.Exit(code=3)

        audit_log.info("cli.login.repair_requested", source="explicit-flag",
                       dry_run=dry_run)
        console.print()
        if dry_run:
            console.print(
                "  [b]Dry run[/] — no config / keychain / backend writes will happen.\n"
            )
        else:
            console.print(
                "  Re-using existing tokens (no browser needed)...\n"
            )

        try:
            result = asyncio.run(_apply_repair(account, dry_run=dry_run))
        except KeyboardInterrupt:
            console.print("\n[dim]cancelled[/]")
            raise typer.Exit(code=130)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗ Backend error during repair:[/] {exc}")
            audit_log.error("cli.login.repair_backend_failed", error=str(exc))
            raise typer.Exit(code=2)

        if dry_run:
            console.print()
            console.print("  [dim](dry run — nothing changed)[/]\n")
        else:
            _print_repair_summary(
                result,
                header=f"Account: {account.email or account.user_id}",
            )
            console.print()
            console.print("  Ready. Run [cyan]flowly[/] to start chatting.\n")
        return

    # ── Path: already signed in — detect gaps, never mutate ──────
    if existing is not None:
        # Auto-provision the account-key provider if it isn't there yet
        # (idempotent, best-effort) so a returning user is billed without
        # dealing with keys — same transparent behaviour as a fresh login.
        _mint_and_save_account_key(existing)
        relay = _check_relay_state()
        is_active, slug = _check_provider_state()
        corruption = check_provider_corruption()
        console.print(
            f"[green]✓[/] Already signed in as "
            f"[b]{existing.email or existing.user_id}[/]."
        )
        # Relay is opt-in: when it isn't wired, ask right here (or honour
        # --relay/--no-relay) instead of bouncing the user to --repair. A
        # "yes" reuses the existing tokens — no browser.
        if not relay.healthy:
            want_relay = relay_opt
            if want_relay is None and sys.stdin.isatty():
                console.print()
                want_relay = typer.confirm(
                    "  Make this bot reachable remotely (phone / Flowly relay)? "
                    "This registers a server",
                    default=False,
                )
            if want_relay:
                try:
                    account = asyncio.run(load_account_refreshing())
                    if account is None:
                        raise RuntimeError("token refresh failed")
                    result = asyncio.run(_apply_repair(account, dry_run=False))
                except Exception as exc:  # noqa: BLE001
                    console.print(
                        f"  [yellow]⚠ Relay registration failed:[/] {exc}\n"
                        f"  Re-run [cyan]flowly login --repair[/] when ready."
                    )
                    audit_log.error("cli.login.relay_optin_failed", error=str(exc))
                    raise typer.Exit(code=2)
                _print_repair_summary(
                    result,
                    header=f"Account: {existing.email or existing.user_id}",
                )
                audit_log.info("cli.login.relay_optin_wired")
                raise typer.Exit()

        gaps: list[str] = []
        if not is_active:
            gaps.append("default LLM provider: [yellow]not set[/]")
        if gaps:
            console.print()
            console.print("  Detected gaps in this install:")
            for g in gaps:
                console.print(f"    [yellow]·[/] {g}")
            console.print()
            console.print(
                "  Fix:  [cyan]flowly login --repair[/]            "
                "[dim](re-wire using existing tokens, no browser)[/]"
            )
            console.print(
                "        [cyan]flowly logout && flowly login[/]    "
                "[dim](full re-login, opens browser)[/]"
            )
            audit_log.info(
                "cli.login.already_signed_in_with_gaps",
                relay_healthy=relay.healthy, relay_reason=relay.reason,
                provider_active=slug,
            )
        else:
            console.print(
                "  Run [cyan]flowly logout[/] to switch accounts."
            )
            audit_log.info("cli.login.already_signed_in_healthy",
                           server_id=relay.server_id)

        # Surface corruption AFTER the gap block — it's an orthogonal
        # warning, not a blocker. ``login --repair`` cannot fix it
        # (the slot's a BYOK slot, not the Flowly slot we write to),
        # so we point at the BYOK setup commands instead.
        if corruption:
            console.print()
            console.print(
                f"  [yellow]⚠ Stale data detected in {len(corruption)} provider slot(s):[/]"
            )
            for issue in corruption:
                console.print(
                    f"    [yellow]·[/] [b]providers.{issue.slot}.{issue.field}[/] — {issue.issue}"
                )
            # Group the per-slot fix hint so identical commands don't
            # repeat for every issue in the same slot.
            slots = sorted({c.slot for c in corruption})
            console.print()
            console.print(
                "  Fix:  re-enter the real API key, or clear the slot if unused:"
            )
            for s in slots:
                console.print(
                    f"        [cyan]flowly setup byok {s} --key <real-{s}-key>[/]"
                )
            console.print(
                "        [dim]or edit ~/.flowly/config.json manually[/]"
            )
            audit_log.info(
                "cli.login.provider_corruption_detected",
                slot_count=len(slots),
                slots=",".join(slots),
            )
        raise typer.Exit()

    # ── Path: fresh sign-in (no tokens, no account) ──────────────
    audit_log.info("cli.login.full_flow_started")

    def _on_code(code: str, url: str) -> None:
        console.print()
        console.print("  Open this URL in your browser:")
        console.print(f"    [cyan underline]{url}[/]")
        console.print()
        if not no_browser:
            try:
                if webbrowser.open(url):
                    console.print("  [dim](opened browser automatically)[/]")
            except Exception:
                pass
        console.print(f"  [dim]Code: {code}  ·  expires in 15 min[/]")
        console.print()

    def _on_status(msg: str) -> None:
        # Spinner is the primary visual indicator — keep extra prints
        # to the bare minimum so they don't compete with the dots.
        pass

    try:
        with console.status("Waiting for approval...", spinner="dots"):
            account = asyncio.run(
                run_login_flow(on_code=_on_code, on_status=_on_status)
            )
    except LoginTimeout:
        console.print(
            "[red]✗ Authorization timed out.[/] "
            "Run [cyan]flowly login[/] again to retry."
        )
        audit_log.info("cli.login.full_flow_timeout")
        raise typer.Exit(code=1)
    except FirebaseAuthError as exc:
        console.print(f"[red]✗ Sign-in failed:[/] {exc}")
        audit_log.error("cli.login.full_flow_auth_failed", error=str(exc))
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/]")
        raise typer.Exit(code=130)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Unexpected error:[/] {exc}")
        audit_log.error("cli.login.full_flow_unexpected", error=str(exc))
        raise typer.Exit(code=1)

    # Provider: ALWAYS auto-provision an account key so the user is billed
    # immediately without ever dealing with keys (Source 0). Best-effort.
    _mint_and_save_account_key(account)

    # Reach: remote / phone access via the relay is OPT-IN — it registers a
    # server. Ask interactively unless the caller forced it with
    # --relay/--no-relay (Desktop / scripts).
    want_relay = relay_opt
    if want_relay is None:
        if sys.stdin.isatty():
            console.print()
            want_relay = typer.confirm(
                "  Make this bot reachable remotely (phone / Flowly relay)? "
                "This registers a server",
                default=False,
            )
        else:
            # Non-interactive (scripts/CI): keep LEGACY behaviour — fresh login
            # always wired the relay before the opt-in question existed, and
            # existing automation depends on that. Opt out with --no-relay.
            want_relay = True

    if want_relay:
        # Same wiring path the standalone --repair uses (server registration +
        # relay config), so post-OAuth wiring matches a recovery wiring.
        try:
            result = asyncio.run(_apply_repair(account, dry_run=False))
        except Exception as exc:  # noqa: BLE001
            # Login succeeded (tokens are in keychain) and the provider key is
            # set — only the relay wiring failed. Don't lose the user.
            console.print(
                f"  [yellow]⚠ Signed in, but relay registration failed:[/] {exc}\n"
                f"  Re-run [cyan]flowly login --repair[/] when ready."
            )
            audit_log.error("cli.login.full_flow_post_register_failed", error=str(exc))
            raise typer.Exit(code=2)
        _print_repair_summary(
            result,
            header=f"Signed in as {account.email or account.user_id}",
        )
    else:
        console.print(
            f"  [green]✓[/] Signed in as [b]{account.email or account.user_id}[/] — "
            "Flowly provider ready, billed to your account (no relay)."
        )

    console.print(
        f"  [green]✓[/] Tokens saved to {credential_storage_status()}"
    )
    console.print()
    console.print("  Ready. Run [cyan]flowly[/] to start chatting.\n")
    audit_log.info("cli.login.full_flow_success", relay=bool(want_relay))
