"""The activity journal on disk: append-only, one file per month.

``<FLOWLY_HOME>/activity/<YYYY-MM>.jsonl`` holds four kinds of line, all
keyed by the task's id (its run id):

- ``start``  written when a task begins, so a task cut short by a crash is
             still known;
- ``task``   written when it ends: the whole record;
- ``recap``  the model's summary, written after the task ended;
- ``seen``   the owner has looked at everything up to ``before`` (ms).

Nothing is rewritten in place. Readers merge the lines, which keeps a write
from a crashing process from ever corrupting an earlier task. Every profile
has its own home, so every bot has its own journal.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

_LOCK = threading.Lock()
_MONTH_FORMAT = "%Y-%m"


def journal_dir() -> Path:
    from flowly.profile import get_flowly_home

    return get_flowly_home() / "activity"


def _month_of(ms: int) -> str:
    return time.strftime(_MONTH_FORMAT, time.gmtime(ms / 1000))


def append(line: dict[str, Any], *, at_ms: int | None = None) -> None:
    """Append one line to the month it belongs to. Never raises."""
    stamp = at_ms if isinstance(at_ms, int) else int(time.time() * 1000)
    try:
        payload = json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        directory = journal_dir()
        with _LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(directory / f"{_month_of(stamp)}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
    except Exception as exc:  # noqa: BLE001 — the journal must never fail a turn
        logger.debug(f"[activity] could not append to the journal: {exc}")


def _month_files() -> list[Path]:
    directory = journal_dir()
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.jsonl") if len(path.stem) == 7)


def read_lines() -> list[dict[str, Any]]:
    """Every well-formed line, oldest month first. A torn last line is skipped."""
    lines: list[dict[str, Any]] = []
    for path in _month_files():
        try:
            with path.open(encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and isinstance(value.get("type"), str):
                        lines.append(value)
        except OSError as exc:
            logger.debug(f"[activity] could not read {path.name}: {exc}")
    return lines


_cache: tuple[tuple[Any, ...], tuple[dict[str, dict[str, Any]], int]] | None = None


def _signature() -> tuple[Any, ...]:
    signature: list[Any] = [str(journal_dir())]
    for path in _month_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((path.name, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


def load() -> tuple[dict[str, dict[str, Any]], int]:
    """``({id: merged task}, seen_before_ms)``.

    A task with a ``start`` but no ``task`` line keeps ``"ended": False`` so the
    reader can tell a running task from one a crash interrupted. The merged
    view is reused until a month file changes; callers must not mutate it.
    """
    global _cache
    signature = _signature()
    if _cache is not None and _cache[0] == signature:
        return _cache[1]
    result = _merge()
    _cache = (signature, result)
    return result


def _merge() -> tuple[dict[str, dict[str, Any]], int]:
    tasks: dict[str, dict[str, Any]] = {}
    seen_before = 0
    for line in read_lines():
        kind = line["type"]
        if kind == "seen":
            before = line.get("before")
            if isinstance(before, int) and before > seen_before:
                seen_before = before
            continue
        task_id = line.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        current = tasks.setdefault(task_id, {"id": task_id, "ended": False})
        if kind == "start":
            for key, value in line.items():
                if key != "type":
                    current.setdefault(key, value)
        elif kind == "task":
            current.update({key: value for key, value in line.items() if key != "type"})
            current["ended"] = True
        elif kind == "recap":
            recap = line.get("recap")
            if isinstance(recap, dict):
                current["recap"] = recap
            usage = line.get("recapTokens")
            if isinstance(usage, dict):
                current["recapTokens"] = usage
    return tasks, seen_before


def prune(retention_days: int, *, now_ms: int | None = None) -> int:
    """Delete month files that ended before the retention window. Returns count."""
    if retention_days is None or retention_days < 0:
        return 0
    now = now_ms if isinstance(now_ms, int) else int(time.time() * 1000)
    cutoff = _month_of(now - retention_days * 86_400_000)
    removed = 0
    with _LOCK:
        for path in _month_files():
            # A month older than the cutoff's month cannot hold a task inside
            # the window; the cutoff's own month is kept whole.
            if path.stem < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.debug(f"[activity] could not prune {path.name}: {exc}")
    return removed
