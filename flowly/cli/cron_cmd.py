"""CLI commands — cron_cmd."""

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
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")


def _ask_running_gateway(method: str, params: dict, *, missing_ok: bool = False) -> tuple[bool, dict | None]:
    """Make a schedule change through the running gateway, if there is one.

    A running gateway holds the schedule in memory and saves it on its own,
    so a change written to jobs.json under it is lost at its next save: a
    removed job kept firing. ``(False, None)`` means no gateway is running and
    the file is the schedule. ``missing_ok`` turns a NOT_FOUND into ``None``.
    """
    from flowly.cli.live_gateway import GatewayRpcError, call_running_gateway

    try:
        return call_running_gateway(method, params)
    except GatewayRpcError as exc:
        if missing_ok and exc.code == "NOT_FOUND":
            return True, None
        console.print(f"[red]The running gateway refused this:[/red] {exc.message}")
        raise typer.Exit(1) from exc


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Include paused, completed, and archived job history",
    ),
):
    """List active schedules, or all lifecycle/history records."""
    from flowly.config.loader import get_data_dir
    from flowly.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all, include_archived=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs and History" if all else "Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time

        styles = {
            "scheduled": "green",
            "paused": "yellow",
            "completed": "cyan",
            "archived": "dim",
        }
        style = styles.get(job.lifecycle, "white")
        status = f"[{style}]{job.lifecycle}[/{style}]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from flowly.config.loader import get_data_dir
    from flowly.cron.service import CronService
    from flowly.cron.types import CronSchedule

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    live, result = _ask_running_gateway("cron.add", {
        "name": name,
        "message": message,
        "schedule": {
            "kind": schedule.kind,
            "atMs": schedule.at_ms,
            "everyMs": schedule.every_ms,
            "expr": schedule.expr,
        },
        "deliver": deliver,
        "to": to,
        "channel": channel,
        "source": "cli",
    })
    if live:
        job = result.get("job") or {}
        console.print(f"[green]✓[/green] Added job '{job.get('name', name)}' ({job.get('id', '?')})")
        return

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
        source="cli",
    )

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to archive"),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Permanently delete the job record and cron output history",
    ),
):
    """Archive a schedule by default, or explicitly purge its cron history."""
    from flowly.config.loader import get_data_dir
    from flowly.cron.service import CronService

    live, result = _ask_running_gateway("cron.remove", {"id": job_id, "purge": purge})
    if live:
        removed = bool(result.get("ok"))
    else:
        store_path = get_data_dir() / "cron" / "jobs.json"
        removed = CronService(store_path).remove_job(job_id, purge=purge)

    if removed:
        action = "Permanently purged" if purge else "Archived"
        console.print(f"[green]✓[/green] {action} job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("output")
def cron_output(
    job_id: str = typer.Argument(..., help="Job ID whose history to read"),
    run_id: str = typer.Option(None, "--run-id", help="Read one exact run"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
):
    """Read retained cron output, including expired-result metadata."""
    from flowly.config.loader import get_data_dir
    from flowly.cron.service import CronService, _OUTPUT_RETENTION_DAYS

    service = CronService(get_data_dir() / "cron" / "jobs.json")
    records = service._read_run_records(job_id, include_content=True)
    if run_id:
        records = [record for record in records if record.get("runId") == run_id]
    records = records[:limit]
    if not records:
        console.print(f"[yellow]No retained result found for {job_id}.[/yellow]")
        return

    for record in records:
        console.rule(
            f"{record.get('jobName', job_id)} · {record.get('runId')} · "
            f"{record.get('status', 'unknown')}"
        )
        if record.get("expired"):
            console.print(
                f"[yellow]Result content expired after {_OUTPUT_RETENTION_DAYS} day(s).[/yellow]"
            )
        else:
            console.print(record.get("content") or "")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from flowly.config.loader import get_data_dir
    from flowly.cron.service import CronService

    live, result = _ask_running_gateway("cron.update", {"id": job_id, "enabled": not disable}, missing_ok=True)
    if live:
        name = (result or {}).get("job", {}).get("name")
    else:
        store_path = get_data_dir() / "cron" / "jobs.json"
        try:
            job = CronService(store_path).enable_job(job_id, enabled=not disable)
        except ValueError as exc:
            console.print(f"[red]Cannot update job:[/red] {exc}")
            raise typer.Exit(1) from exc
        name = job.name if job else None
    if name:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID or name to run"),
    force: bool = typer.Option(
        False,
        "--force/--no-force",
        "-f",
        help="Run paused/completed jobs without reactivating their schedule",
    ),
    port: int = typer.Option(0, "--port", "-p", help="Gateway port (default: config.gateway.port)."),
):
    """Run one manual attempt without changing the schedule lifecycle."""
    if not port:
        from flowly.integrations.gateway_autostart import _configured_port

        port = _configured_port()
    import json
    import urllib.request
    import urllib.error

    payload = json.dumps({"job_id": job_id, "force": force}).encode()
    req = urllib.request.Request(
        f"http://localhost:{port}/api/cron/run",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=130) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                console.print(f"[green]✓[/green] Job '{job_id}' triggered via gateway")
            else:
                console.print(f"[red]Failed:[/red] {body.get('error', 'unknown')}")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        console.print(f"[red]Failed:[/red] {body.get('error', str(e))}")
    except urllib.error.URLError:
        console.print(
            "[red]Gateway not reachable.[/red] Is 'flowly run' running?\n"
            f"Tried http://localhost:{port}/api/cron/run"
        )
