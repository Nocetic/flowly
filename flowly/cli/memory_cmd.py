"""CLI commands — memory governance (list/review/accept/reject/correct/undo/...).

Thin Typer wrappers over flowly.memory.coordinator.MemoryGovernance. All
lifecycle logic lives in the facade; this file just resolves paths, wires the
store + KG, and renders output.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

console = Console()

memory_app = typer.Typer(help="Inspect and correct long-term memory")


def _open():
    """Open a MemoryGovernance facade against the active profile's stores."""
    from flowly.config.loader import load_config, get_data_dir
    from flowly.agent.memory import MemoryStore
    from flowly.memory.governance import GovernanceStore
    from flowly.memory.coordinator import MemoryGovernance
    from flowly.memory.kg_mirror import SqliteKGMirror

    config = load_config()
    workspace = config.workspace_path
    # state_dir MUST match the runtime (gateway uses get_data_dir()), otherwise
    # the CLI reads a different governance db than the live agent writes.
    state_dir = get_data_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    gov = GovernanceStore(state_dir / "memory_governance.sqlite3")
    memory_store = MemoryStore(workspace)
    kg_path = state_dir / "knowledge_graph.sqlite3"

    def _kg_summary() -> str:
        if not kg_path.exists():
            return ""
        try:
            from flowly.memory.knowledge_graph import KnowledgeGraph
            return KnowledgeGraph(str(kg_path)).summary(max_entities=20)
        except Exception:
            return ""

    mirror = SqliteKGMirror(str(kg_path)) if kg_path.exists() else None
    return MemoryGovernance(
        gov, memory_store=memory_store, kg_mirror=mirror, kg_summary_fn=_kg_summary
    )


def _render(items) -> None:
    if not items:
        console.print("[dim]no items[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("status")
    table.add_column("kind")
    table.add_column("conf", justify="right")
    table.add_column("text")
    for it in items:
        table.add_row(
            it.id, it.status, it.kind, f"{it.confidence:.2f}",
            (it.text[:80] + "…") if len(it.text) > 80 else it.text,
        )
    console.print(table)


@memory_app.command("list")
def list_cmd(
    status: str = typer.Option(None, "--status", "-s", help="Filter by status"),
):
    """List memory items (optionally by status)."""
    mg = _open()
    _render(mg.list_items(status=status))


@memory_app.command("review")
def review_cmd():
    """Show the needs-review queue."""
    mg = _open()
    items = mg.review_queue()
    _render(items)
    if items:
        console.print(
            f"\n[dim]{len(items)} item(s) awaiting review. "
            f"Use 'flowly memory accept/reject/correct <id>'.[/dim]"
        )


@memory_app.command("accept")
def accept_cmd(item_id: str):
    """Approve a queued item (→ active)."""
    out = _open().accept(item_id)
    console.print(f"[green]accepted[/green] {out.id} → {out.status}")


@memory_app.command("reject")
def reject_cmd(item_id: str):
    """Reject an item (→ rejected)."""
    out = _open().reject(item_id)
    console.print(f"[yellow]rejected[/yellow] {out.id}")


@memory_app.command("feedback")
def feedback_cmd(
    item_id: str,
    helpful: bool = typer.Option(None, "--helpful/--unhelpful", help="Was the item helpful?"),
    note: str = typer.Option("", "--note", help="Optional reason."),
):
    """Record trust feedback on a memory item (adjusts its confidence)."""
    if helpful is None:
        console.print("[red]specify --helpful or --unhelpful[/red]")
        raise typer.Exit(1)
    mg = _open()
    out = mg.ingest_feedback(item_id, helpful, note)
    mg.refresh()  # CLI: regenerate now (no end-of-turn hook here)
    console.print(
        f"[green]feedback recorded[/green] {out.id} → confidence {out.confidence:.2f}, "
        f"status {out.status}"
    )


@memory_app.command("correct")
def correct_cmd(item_id: str, text: str):
    """Edit an item's text (and activate it)."""
    out = _open().correct(item_id, text)
    console.print(f"[green]corrected[/green] {out.id} → {out.status}: {out.text}")


@memory_app.command("undo")
def undo_cmd(item_id: str):
    """Restore a superseded/stale item to active (rollback)."""
    out = _open().undo(item_id)
    console.print(f"[green]restored[/green] {out.id} → {out.status}")


@memory_app.command("refresh")
def refresh_cmd():
    """Regenerate the MEMORY.md generated block from active items."""
    out = _open().refresh()
    if out is None:
        console.print("[yellow]no memory store available[/yellow]")
    else:
        console.print("[green]MEMORY.md regenerated[/green]")


@memory_app.command("status")
@memory_app.command("stats")
def stats_cmd():
    """Show memory statistics."""
    s = _open().stats()
    console.print(f"[bold]total[/bold] {s['total']}  "
                  f"[green]active[/green] {s['active']}  "
                  f"[yellow]review[/yellow] {s['review_queue']}")
    if s["by_status"]:
        console.print("by status: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_status"].items())))
    if s["by_kind"]:
        console.print("by kind:   " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_kind"].items())))


@memory_app.command("consolidate")
def consolidate_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show proposed cleanup without applying."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Print the raw LLM output (debug)."
    ),
):
    """LLM-propose + apply semantic cleanup: merge cross-key duplicate facts,
    retire free-form that duplicates the KG, mark outdated free-form stale."""
    import asyncio
    import json

    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.memory.consolidate import PROMPT, Consolidator, parse_operations

    config = load_config()
    ap = resolve_active_provider(config)
    if ap is None:
        console.print("[red]No LLM provider configured.[/red] Set one in /integrations.")
        raise typer.Exit(1)

    model = config.agents.defaults.model
    _last_raw = {"text": ""}

    def _propose(ctx: dict):
        from flowly.providers.factory import build_provider
        provider = build_provider(ap, default_model=model, config=config)
        prompt = PROMPT.replace(
            "{context}", json.dumps(ctx, ensure_ascii=False, indent=2)
        )

        async def _stream() -> str:
            # Stream (not chat()): the Flowly proxy 504s on long non-streamed
            # completions — streaming keeps the connection alive like the agent.
            parts: list[str] = []
            async for delta in provider.chat_stream(
                [{"role": "user", "content": prompt}],
                model=model, max_tokens=2048, temperature=0.1,
            ):
                if delta.content:
                    parts.append(delta.content)
            return "".join(parts)

        text = asyncio.run(_stream())
        _last_raw["text"] = text
        return parse_operations(text)

    mg = _open()  # reuse facade's resolved gov + kg + memory_store
    consolidator = Consolidator(
        mg.gov, _propose, kg_mirror=mg.kg_mirror,
        memory_store=mg.memory_store, kg_summary_fn=mg.kg_summary_fn,
    )
    console.print(f"[dim]proposing via {ap.key}/{model}…[/dim]")
    ops, res = consolidator.run(dry_run=dry_run)

    if raw or not ops:
        rt = _last_raw["text"]
        console.print(f"[dim]── raw LLM output ({len(rt)} chars) ──[/dim]")
        console.print(rt or "[red](empty — provider returned nothing)[/red]")
        console.print("[dim]── end raw ──[/dim]")
    if not ops:
        console.print("[yellow]no operations parsed[/yellow]")
        return
    for op in ops:
        tail = f" → {op.into_id}" if op.into_id else ""
        console.print(f"  [cyan]{op.op}[/cyan] {op.item_id}{tail}  [dim]{op.reason}[/dim]")
    if dry_run:
        console.print(f"\n[yellow]dry-run[/yellow] — {len(ops)} proposed, nothing applied. "
                      f"Re-run without --dry-run to apply.")
    else:
        console.print(f"\n[green]applied[/green] merged={res.merged} superseded={res.superseded} "
                      f"staled={res.staled} skipped={res.skipped}")
        for e in res.errors:
            console.print(f"  [dim]skip: {e}[/dim]")


@memory_app.command("migrate")
def migrate_cmd():
    """Import legacy MEMORY.md entries into governance (one-time, idempotent)."""
    from flowly.config.loader import load_config, get_data_dir
    from flowly.agent.memory import MemoryStore
    from flowly.memory.governance import GovernanceStore
    from flowly.memory.migration import kg_value_tokens, migrate_memory_md

    config = load_config()
    workspace = config.workspace_path
    state_dir = get_data_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    gov = GovernanceStore(state_dir / "memory_governance.sqlite3")
    ms = MemoryStore(workspace)

    tokens = set()
    kg_path = state_dir / "knowledge_graph.sqlite3"
    if kg_path.exists():
        try:
            from flowly.memory.knowledge_graph import KnowledgeGraph
            tokens = kg_value_tokens(KnowledgeGraph(str(kg_path)))
        except Exception:
            pass

    res = migrate_memory_md(gov, ms, kg_tokens=tokens)
    if not res.migrated:
        console.print(f"[yellow]skipped[/yellow] ({res.reason})")
    else:
        console.print(
            f"[green]migrated[/green] imported={res.imported} "
            f"kg_skipped={res.kg_skipped} duplicates={res.duplicates}\n"
            f"backup: {res.backup_path}"
        )
