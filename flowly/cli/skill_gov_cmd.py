"""CLI — skill self-improvement governance (`flowly skill ...`).

Thin wrappers over flowly.skills.governance.SkillGovernance. Deterministic
commands (log/undo/rollback/usage/list/archive/restore/stale) need no LLM and
work offline; mine/curate (added with the miner/curator) call the provider.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

console = Console()
skill_gov_app = typer.Typer(help="Inspect and govern the agent's self-improved skills")


def _open():
    from flowly.config.loader import get_data_dir
    from flowly.profile import get_flowly_home
    from flowly.agent.skill_usage import SkillUsageStore
    from flowly.agent.tools.skill_manage import SkillManageTool
    from flowly.skills.op_log import SkillOpLog
    from flowly.skills.snapshot import SkillSnapshots
    from flowly.skills.governance import SkillGovernance

    skills = get_flowly_home() / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    return SkillGovernance(
        SkillOpLog(get_data_dir() / "skill_governance.sqlite3"),
        SkillUsageStore(skills),
        SkillManageTool(),
        SkillSnapshots(skills_dir=skills),
    )


@skill_gov_app.command("usage")
def usage_cmd():
    """Show per-skill usage + lifecycle state."""
    rows = _open().usage_report()
    if not rows:
        console.print("[dim]no skill usage recorded yet[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    for c in ("name", "uses", "state", "provenance", "pinned", "last used"):
        t.add_column(c)
    for r in sorted(rows, key=lambda x: -x.get("use_count", 0)):
        t.add_row(r["name"], str(r.get("use_count", 0)), r.get("state", ""),
                  r.get("provenance", ""), "yes" if r.get("pinned") else "",
                  (r.get("last_used_at") or "")[:19])
    console.print(t)


@skill_gov_app.command("log")
def log_cmd(status: str = typer.Option(None, "--status", "-s")):
    """Show the skill operation history."""
    ops = _open().list_ops(status=status)
    if not ops:
        console.print("[dim]no operations[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    for c in ("id", "kind", "status", "targets", "rationale"):
        t.add_column(c, overflow="fold")
    for o in ops:
        tgt = ", ".join(o.targets) or o.draft_name
        t.add_row(o.id, o.kind, o.status, tgt, (o.rationale or "")[:60])
    console.print(t)


@skill_gov_app.command("undo")
def undo_cmd(op_id: str):
    """Reverse a single applied op."""
    console.print(asyncio.run(_open().undo(op_id)))


@skill_gov_app.command("rollback")
def rollback_cmd(snapshot_id: str = typer.Option(None, "--id")):
    """Restore the whole skill tree from a snapshot (latest if --id omitted)."""
    console.print(_open().rollback(snapshot_id))


@skill_gov_app.command("archive")
def archive_cmd(name: str):
    """Archive a skill (move to skills_archive/; restorable)."""
    console.print(asyncio.run(_open().archive(name)))


@skill_gov_app.command("restore")
def restore_cmd(name: str):
    """Restore an archived skill."""
    console.print(asyncio.run(_open().restore(name)))


@skill_gov_app.command("stale")
def stale_cmd():
    """Run the deterministic age-based staling pass."""
    res = _open().run_staling()
    console.print(f"checked {res.checked}, marked stale {res.marked_stale}")


def _improve_tool():
    """Build a SkillImproveTool against the active provider (streams, no 504)."""
    from flowly.config.loader import load_config, get_data_dir
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.providers.factory import build_provider
    from flowly.profile import get_flowly_home
    from flowly.agent.skills import SkillsLoader
    from flowly.agent.skill_usage import SkillUsageStore
    from flowly.memory.dreamer import SessionIndexDeltaSource
    from flowly.agent.tools.skill_improve import SkillImproveTool

    config = load_config()
    ap = resolve_active_provider(config)
    if ap is None:
        console.print("[red]No LLM provider configured.[/red]")
        raise typer.Exit(1)
    model = config.agents.defaults.model
    provider = build_provider(ap, default_model=model, config=config)
    si = config.agents.defaults.skill_improvement
    gov = _open()
    skills = get_flowly_home() / "skills"
    return SkillImproveTool(
        facade=gov, provider=provider, model=model,
        delta_source=SessionIndexDeltaSource(str(get_data_dir() / "session_index.sqlite")),
        skills_loader=SkillsLoader(config.workspace_path),
        usage=SkillUsageStore(skills),
        min_evidence_sessions=si.min_evidence_sessions,
        min_repeat_count=si.min_repeat_count,
        max_messages=si.max_messages_per_run,
    )


@skill_gov_app.command("mine")
def mine_cmd(dry_run: bool = typer.Option(False, "--dry-run", "-n")):
    """Mine recent conversations for recurring procedures → new skills."""
    console.print(asyncio.run(_improve_tool().execute(mode="mine", dry_run=dry_run)))


@skill_gov_app.command("curate")
def curate_cmd(dry_run: bool = typer.Option(False, "--dry-run", "-n")):
    """Consolidate the skill library (merge/demote/archive)."""
    console.print(asyncio.run(_improve_tool().execute(mode="curate", dry_run=dry_run)))
