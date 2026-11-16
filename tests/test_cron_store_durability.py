"""A schedule file cut short by a crash never takes the agent down (2026-11-16).

On a Windows test machine ``cron/jobs.json`` came back empty after the agent
had been stopped and started many times in a row. Loading it raised
``CronStoreLoadError`` from the gateway's startup, so the agent exited before it
listened, its service started it again, and it never came up: the app showed
it offline for good.

- The file is written to the disk before it replaces the old one, so a crash
  or a power cut leaves the old schedules or the new, never an empty file.
- A blank file (no bytes, only whitespace, only NUL bytes) holds no schedule:
  it is set aside and the agent starts with none.
- A file with something unreadable in it still fails closed, untouched
  (tests/test_cron_result_retention.py), and the gateway runs without schedules.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from flowly.cron import service as cron_service
from flowly.cron.service import CronService, CronStoreLoadError
from flowly.cron.types import CronSchedule

GATEWAY_CMD = Path(cron_service.__file__).resolve().parents[1] / "cli" / "gateway_cmd.py"


@pytest.mark.parametrize("raw", [b"", b"   \r\n", b"\x00" * 4096], ids=["empty", "whitespace", "nul-bytes"])
def test_a_blank_store_is_set_aside_and_the_service_starts_with_no_jobs(tmp_path: Path, raw: bytes) -> None:
    store = tmp_path / "jobs.json"
    store.write_bytes(raw)

    service = CronService(store)
    assert service.list_jobs(include_disabled=True, include_archived=True) == []

    aside = list(tmp_path.glob("jobs.json.blank-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == raw
    job = service.add_job("after", CronSchedule(kind="every", every_ms=60_000), "hello")
    assert [j["id"] for j in json.loads(store.read_text(encoding="utf-8"))["jobs"]] == [job.id]


@pytest.mark.asyncio
async def test_a_blank_store_lets_the_scheduler_start(tmp_path: Path) -> None:
    store = tmp_path / "jobs.json"
    store.write_bytes(b"")
    service = CronService(store)
    await service.start()
    try:
        assert service.status()["enabled"] is True
        assert json.loads(store.read_text(encoding="utf-8"))["jobs"] == []
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_an_unreadable_store_leaves_the_scheduler_off_and_the_file_untouched(tmp_path: Path) -> None:
    store = tmp_path / "jobs.json"
    store.write_text("{broken json", encoding="utf-8")
    service = CronService(store)
    with pytest.raises(CronStoreLoadError):
        await service.start()
    assert service._running is False
    assert store.read_text(encoding="utf-8") == "{broken json"
    assert not list(tmp_path.glob("jobs.json.blank-*"))


def test_the_store_reaches_the_disk_before_it_replaces_the_old_one(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    real_fsync, real_replace = cron_service.os.fsync, cron_service.os.replace

    def fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def replace(src: str, dst: str) -> None:
        events.append(f"replace:{Path(dst).name}")
        real_replace(src, dst)

    monkeypatch.setattr(cron_service.os, "fsync", fsync)
    monkeypatch.setattr(cron_service.os, "replace", replace)

    service = CronService(tmp_path / "jobs.json")
    service.add_job("durable", CronSchedule(kind="every", every_ms=60_000), "hello")

    saves = [i for i, event in enumerate(events) if event == "replace:jobs.json"]
    assert saves, events
    for index in saves:
        assert events[index - 1] == "fsync", events
    assert not list(tmp_path.glob("jobs.tmp.*"))


def _guarded_by_store_error(tree: ast.AST, call: ast.Call) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(call in list(ast.walk(statement)) for statement in node.body):
            continue
        for handler in node.handlers:
            names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            if any(isinstance(name, ast.Name) and name.id == "CronStoreLoadError" for name in names):
                return True
    return False


def test_the_gateway_starts_without_schedules_when_their_store_is_unreadable() -> None:
    """Every schedule-store read in the gateway's startup is caught: the schedules go off, the agent stays up."""
    tree = ast.parse(GATEWAY_CMD.read_text(encoding="utf-8"))
    gateway = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "gateway")
    calls = [
        node for node in ast.walk(gateway)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "cron"
        and node.func.attr in {"status", "start"}
    ]
    assert {c.func.attr for c in calls} == {"status", "start"}
    unguarded = [f"cron.{c.func.attr}() line {c.lineno}" for c in calls if not _guarded_by_store_error(gateway, c)]
    assert not unguarded, unguarded
