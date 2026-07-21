"""``flowly doctor`` offline diagnostics runner.

The default command is observational: it reads files directly, never through
self-healing runtime loaders, and does not open sockets, keychains, or service
control processes. Explicit repair and online modes are layered on separately
so their authority cannot leak into the default path.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from flowly.diagnostics.checks import (
    CHECKS,
    check_account_snapshot,
    check_channels,
    check_config_file,
    check_config_permissions,
    check_config_validity,
    check_duplicate_keys,
    check_gateway_security,
    check_memory,
    check_model,
    check_online_gateway,
    check_online_provider,
    check_provider,
    check_provider_corruption,
    check_relay,
    check_runtime_stores,
    check_service_definition,
    check_sessions,
    check_state_directory,
    check_unknown_config_keys,
    check_workspace,
)
from flowly.diagnostics.models import (
    DoctorCheck,
    DoctorContext,
    DoctorResult,
    RepairRisk,
    Status,
)
from flowly.diagnostics.repairs import (
    RepairOutcome,
    apply_safe_fixes,
    repair_config_backup,
    repair_config_duplicates,
    repair_memory,
    repair_sessions,
)

console = Console()


def _doctor_data_dir() -> Path:
    from flowly.profile import get_flowly_home

    return get_flowly_home()


def _doctor_config_path() -> Path:
    return _doctor_data_dir() / "config.json"


def _selected_checks(categories: set[str] | None, *, online: bool) -> list[DoctorCheck]:
    available = [check for check in CHECKS if online or not check.online_only]
    if not categories:
        return available
    known = {check.category for check in CHECKS}
    invalid = categories - known
    if invalid:
        raise ValueError(f"unknown doctor categories: {', '.join(sorted(invalid))}")
    if "online" in categories and not online:
        raise ValueError("doctor category 'online' requires --online")
    selected = [check for check in available if check.category in categories]
    prerequisite_names = {
        "state_dir",
        "config_file",
        "config_permissions",
        "config_validity",
    }
    prerequisites = [check for check in available if check.name in prerequisite_names]
    seen: set[str] = set()
    return [
        check
        for check in prerequisites + selected
        if not (check.name in seen or seen.add(check.name))
    ]


def run_doctor(
    fix: bool = False,
    *,
    online: bool = False,
    strict: bool = False,
    json_output: bool = False,
    categories: set[str] | None = None,
    timeout: float = 5.0,
    repair: str = "",
) -> int:
    """Run Doctor and return a stable automation-oriented exit code.

    ``0`` means no blocking/internal failure, ``1`` means a diagnosed blocking
    problem, and ``2`` means Doctor itself could not complete a check or the
    invocation requested a mode that is not available safely yet. ``--strict``
    promotes warnings to exit code ``1``.
    """
    if timeout <= 0 or timeout > 120:
        return _invocation_error(
            "timeout must be greater than 0 and at most 120 seconds",
            json_output=json_output,
        )
    if fix and repair:
        return _invocation_error("--fix and --repair are mutually exclusive", json_output=json_output)
    if fix and online:
        return _invocation_error("--fix and --online are mutually exclusive", json_output=json_output)
    if repair:
        return _run_named_repair(repair, json_output=json_output)

    try:
        checks = _selected_checks(categories, online=online)
    except ValueError as exc:
        return _invocation_error(str(exc), json_output=json_output)

    ctx = _new_context(read_only=not fix, timeout=timeout, online=online)
    if not json_output:
        mode = "safe auto-repair" if fix else "read-only"
        connectivity = "online probes" if online else "offline"
        console.print(f"[bold cyan]flowly doctor[/bold cyan]  ({mode}, {connectivity})\n")

    _execute_checks(ctx, checks)
    if fix:
        ctx = _apply_fix_passes(ctx, checks, timeout=timeout)

    if json_output:
        _print_json(ctx, strict=strict)
    else:
        _print_report(ctx, strict=strict)

    if any(result.status == Status.INTERNAL for result in ctx.results):
        return 2
    if any(result.status == Status.ERROR for result in ctx.results):
        return 1
    if strict and any(result.status == Status.WARN for result in ctx.results):
        return 1
    return 0


def _new_context(*, read_only: bool, timeout: float, online: bool = False) -> DoctorContext:
    return DoctorContext(
        config_path=_doctor_config_path(),
        data_dir=_doctor_data_dir(),
        fix=not read_only,
        read_only=read_only,
        online=online,
        timeout=timeout,
    )


def _execute_checks(ctx: DoctorContext, checks: list[DoctorCheck]) -> None:

    for check in checks:
        ctx.current_category = check.category
        before = len(ctx.results)
        started = time.perf_counter()
        try:
            check.function(ctx)
        except Exception as exc:  # One check must never suppress later checks.
            ctx.internal(check.name, exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        for result in ctx.results[before:]:
            result.duration_ms = duration_ms


def _apply_fix_passes(
    initial: DoctorContext,
    checks: list[DoctorCheck],
    *,
    timeout: float,
) -> DoctorContext:
    outcomes: list[RepairOutcome] = []
    failures: list[tuple[str, Exception]] = []
    ctx = initial
    # Missing state → missing config → missing workspace can require successive
    # snapshots. The cap makes a faulty/non-idempotent repair impossible to loop.
    for _ in range(4):
        pass_outcomes, pass_failures = apply_safe_fixes(ctx)
        outcomes.extend(pass_outcomes)
        failures.extend(pass_failures)
        if not pass_outcomes or pass_failures:
            break
        ctx = _new_context(read_only=False, timeout=timeout, online=initial.online)
        _execute_checks(ctx, checks)

    if outcomes:
        ctx = _new_context(read_only=False, timeout=timeout, online=initial.online)
        _execute_checks(ctx, checks)
    fixed_results: list[DoctorResult] = []
    for outcome in outcomes:
        result = DoctorResult(
            name=outcome.name,
            status=Status.FIXED,
            message=outcome.message,
            category="repair",
            changed_paths=[str(path) for path in outcome.changed_paths],
        )
        fixed_results.append(result)
    ctx.results = fixed_results + ctx.results
    ctx.current_category = "repair"
    for name, exc in failures:
        ctx.internal(f"fix_{name}", exc)
    return ctx


def _run_named_repair(repair: str, *, json_output: bool) -> int:
    actions = {
        "config_backup": lambda: repair_config_backup(_doctor_config_path()),
        "config_duplicates": lambda: repair_config_duplicates(_doctor_config_path()),
        "memory_regenerate": lambda: repair_memory(_doctor_data_dir(), _doctor_config_path()),
        "session_salvage": lambda: repair_sessions(_doctor_data_dir()),
    }
    action = actions.get(repair)
    if action is None:
        return _invocation_error(
            f"unknown repair '{repair}'; choose: {', '.join(sorted(actions))}",
            json_output=json_output,
        )
    try:
        outcome = action()
    except Exception as exc:
        payload = {
            "schemaVersion": 1,
            "status": "internal",
            "repair": repair,
            "error": f"repair failed ({type(exc).__name__}); active data was preserved or rolled back",
        }
        if json_output:
            _write_json(payload)
        else:
            console.print(f"[red]{payload['error']}[/red]")
        return 2
    payload = {
        "schemaVersion": 1,
        "status": "repaired" if outcome.changed_paths else "healthy",
        "repair": repair,
        "message": outcome.message,
        "changedPaths": [str(path) for path in outcome.changed_paths],
    }
    if json_output:
        _write_json(payload)
    else:
        console.print(f"[green]{outcome.message}[/green]")
        for path in outcome.changed_paths:
            console.print(f"  [dim]{path}[/dim]")
    return 0


def _invocation_error(message: str, *, json_output: bool) -> int:
    if json_output:
        _write_json({"schemaVersion": 1, "status": "internal", "error": message})
    else:
        console.print(f"[red]{message}[/red]")
    return 2


def _summary(ctx: DoctorContext) -> dict[str, int]:
    return {
        status.value: sum(result.status == status for result in ctx.results) for status in Status
    }


def _overall_status(ctx: DoctorContext, *, strict: bool) -> str:
    summary = _summary(ctx)
    if summary[Status.INTERNAL.value]:
        return "internal"
    if summary[Status.ERROR.value] or (strict and summary[Status.WARN.value]):
        return "unhealthy"
    if summary[Status.WARN.value]:
        return "degraded"
    return "healthy"


def _print_json(ctx: DoctorContext, *, strict: bool) -> None:
    _write_json(
        {
            "schemaVersion": 1,
            "status": _overall_status(ctx, strict=strict),
            "readOnly": ctx.read_only,
            "online": ctx.online,
            "profileHome": str(ctx.data_dir),
            "summary": _summary(ctx),
            "results": [result.to_dict() for result in ctx.results],
        }
    )


def _write_json(payload: dict[str, Any]) -> None:
    """Write one physical JSON line without Rich terminal wrapping."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _print_report(ctx: DoctorContext, *, strict: bool) -> None:
    icons = {
        Status.OK: "[green]  ✓[/green]",
        Status.WARN: "[yellow]  ⚠[/yellow]",
        Status.ERROR: "[red]  ✗[/red]",
        Status.FIXED: "[cyan]  ✦[/cyan]",
        Status.SKIPPED: "[dim]  -[/dim]",
        Status.INTERNAL: "[magenta]  ![/magenta]",
    }
    for result in ctx.results:
        console.print(f"{icons[result.status]}  [bold]{result.name}[/bold]  {result.message}")
        if result.detail:
            for line in result.detail.splitlines():
                console.print(f"      [dim]{line}[/dim]")
        if (
            ctx.read_only
            and result.fixable
            and result.repair_command
            and result.status in (Status.WARN, Status.ERROR)
        ):
            console.print(
                f"      [dim]→ {result.repair_command} (risk={result.risk.value})[/dim]"
            )

    counts = _summary(ctx)
    parts = [
        f"[red]{counts['error']} error(s)[/red]" if counts["error"] else "",
        f"[magenta]{counts['internal']} internal failure(s)[/magenta]"
        if counts["internal"]
        else "",
        f"[yellow]{counts['warn']} warning(s)[/yellow]" if counts["warn"] else "",
        f"[green]{counts['ok']} ok[/green]" if counts["ok"] else "",
        f"[dim]{counts['skipped']} skipped[/dim]" if counts["skipped"] else "",
    ]
    console.print()
    console.print("  " + "  ·  ".join(part for part in parts if part))
    if strict and counts["warn"]:
        console.print("  [dim]--strict promotes warnings to exit code 1[/dim]")


__all__ = [
    "CHECKS",
    "DoctorCheck",
    "DoctorContext",
    "DoctorResult",
    "RepairRisk",
    "Status",
    "run_doctor",
    "check_account_snapshot",
    "check_channels",
    "check_config_file",
    "check_config_permissions",
    "check_config_validity",
    "check_duplicate_keys",
    "check_gateway_security",
    "check_memory",
    "check_model",
    "check_online_gateway",
    "check_online_provider",
    "check_provider",
    "check_provider_corruption",
    "check_relay",
    "check_runtime_stores",
    "check_service_definition",
    "check_sessions",
    "check_state_directory",
    "check_unknown_config_keys",
    "check_workspace",
]
