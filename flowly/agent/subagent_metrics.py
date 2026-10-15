"""What subagents cost and how they went, kept after their runs are pruned.

Each run that ends adds one line to ``metrics.jsonl`` next to the run index:
when it ran, who started it, how it ended, how long it took and the tokens
it used. Never the brief, the result or the conversation — a metric line is
safe to keep long after the run itself is gone (runs are pruned after 30
days; these lines are trimmed by size instead).

``summarize`` turns the lines into what the owner sees: totals, outcomes,
durations and tokens for a period, day by day.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from flowly.agent.subagent_registry import SubagentRunRecord

METRICS_FILE = "metrics.jsonl"
# Past this size the file keeps its newest half.
_MAX_BYTES = 8 * 1024 * 1024
_OUTCOMES = ("ok", "error", "timeout", "cancelled", "interrupted")


def metrics_path(registry_path: Path) -> Path:
    return registry_path.parent / METRICS_FILE


def metric_line(record: SubagentRunRecord) -> dict[str, Any]:
    """One ended run as a metric: numbers and kinds, never its content."""
    started = record.started_at or record.created_at
    ended = record.ended_at or time.time()
    return {
        "v": 1,
        "runId": record.run_id,
        "endedAt": round(ended, 3),
        "createdAt": round(record.created_at, 3),
        "durationS": round(max(0.0, ended - started), 3),
        "origin": record.origin,
        "kind": record.kind,
        "model": record.model,
        "outcome": record.outcome or "unknown",
        "errorCode": record.error_code,
        "toolCalls": len(record.tool_trace),
        "tokens": {key: int(record.usage.get(key, 0)) for key in ("input", "output", "cacheRead", "total")},
        "channel": record.parent_channel,
    }


def append_metric(path: Path, record: SubagentRunRecord) -> None:
    """Add one ended run; a metric never fails the run it describes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(metric_line(record), ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        if path.stat().st_size > _MAX_BYTES:
            _keep_newest_half(path)
    except OSError:
        logger.warning("[subagent-metrics] could not record a run")


def _keep_newest_half(path: Path) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    kept = b"".join(lines[len(lines) // 2:])
    tmp = path.with_name(path.name + ".tmp." + secrets.token_hex(6))
    tmp.write_bytes(kept)
    os.replace(tmp, path)


def read_metrics(path: Path) -> list[dict[str, Any]]:
    """Every readable line; a torn or foreign line is skipped, not fatal."""
    if not path.exists():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("endedAt"), (int, float)):
            rows.append(row)
    return rows


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summarize(
    rows: Iterable[dict[str, Any]], *, days: int, now: float | None = None, include_system: bool = False,
) -> dict[str, Any]:
    """Totals, outcomes, durations and tokens for the last ``days`` days, and per day (UTC)."""
    now = time.time() if now is None else now
    since = now - days * 86_400
    picked = [
        row for row in rows
        if row["endedAt"] >= since and (include_system or row.get("origin") != "system")
    ]
    # One line per run: a run recorded twice (a restart between its end and
    # the next read) counts once, as last recorded.
    by_run: dict[str, dict[str, Any]] = {}
    for row in picked:
        by_run[str(row.get("runId"))] = row
    picked = sorted(by_run.values(), key=lambda row: row["endedAt"])

    def tokens(row: dict[str, Any], key: str) -> int:
        value = (row.get("tokens") or {}).get(key, 0)
        return int(value) if isinstance(value, (int, float)) else 0

    outcomes = {name: 0 for name in _OUTCOMES}
    days_out: dict[str, dict[str, Any]] = {}
    for row in picked:
        outcome = str(row.get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        day = datetime.fromtimestamp(row["endedAt"], tz=timezone.utc).strftime("%Y-%m-%d")
        bucket = days_out.setdefault(day, {"date": day, "runs": 0, "ok": 0, "failed": 0, "tokens": 0})
        bucket["runs"] += 1
        bucket["ok"] += outcome == "ok"
        bucket["failed"] += outcome in ("error", "timeout")
        bucket["tokens"] += tokens(row, "total")
    durations = [float(row.get("durationS") or 0) for row in picked]
    return {
        "days": days,
        "since": since,
        "runs": len(picked),
        "byOrigin": {
            origin: sum(1 for row in picked if row.get("origin") == origin)
            for origin in ("owner", "agent") + (("system",) if include_system else ())
        },
        "outcomes": outcomes,
        "durationS": {
            "average": round(sum(durations) / len(durations), 1) if durations else None,
            "p50": _percentile(durations, 0.5),
            "p90": _percentile(durations, 0.9),
        },
        "tokens": {key: sum(tokens(row, key) for row in picked) for key in ("input", "output", "cacheRead", "total")},
        "toolCalls": sum(int(row.get("toolCalls") or 0) for row in picked),
        "byDay": [days_out[key] for key in sorted(days_out)],
    }
