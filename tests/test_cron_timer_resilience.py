"""M1 — a cron tick failure must not kill the self-perpetuating timer.

The timer chain is `_arm_timer` → fire-and-forget `tick()` → `_on_timer` →
`_run_due_jobs` → (re-arm). If `_save_store()` raises mid-tick (full / read-only
disk), the exception used to escape `_run_due_jobs` *before* the re-arm, killing
the tick task — so the timer was never rescheduled and ALL cron jobs silently
stopped until restart. `_on_timer` must catch the failure and always re-arm.
"""

from __future__ import annotations

import asyncio

from flowly.cron.service import CronService


def _bare_service(tmp_path):
    """A CronService with only the attributes `_on_timer` touches (no full init)."""
    svc = CronService.__new__(CronService)
    svc._executing = False
    svc._store = object()           # truthy → past the early return
    svc._running = True
    svc.store_path = tmp_path / "cron" / "store.json"
    svc.store_path.parent.mkdir(parents=True, exist_ok=True)
    return svc


def test_tick_failure_still_rearms_timer(tmp_path, monkeypatch):
    svc = _bare_service(tmp_path)
    armed = {"n": 0}

    async def _boom():
        raise RuntimeError("simulated _save_store failure (disk full)")

    monkeypatch.setattr(svc, "_run_due_jobs", _boom)
    monkeypatch.setattr(svc, "_arm_timer", lambda: armed.__setitem__("n", armed["n"] + 1))

    # Must NOT raise, and must re-arm despite the failed tick.
    asyncio.run(svc._on_timer())
    assert armed["n"] == 1


def test_normal_tick_rearms_exactly_once(tmp_path, monkeypatch):
    svc = _bare_service(tmp_path)
    armed = {"n": 0}

    async def _ok():
        return None

    monkeypatch.setattr(svc, "_run_due_jobs", _ok)
    monkeypatch.setattr(svc, "_arm_timer", lambda: armed.__setitem__("n", armed["n"] + 1))

    asyncio.run(svc._on_timer())
    # Exactly once — the re-arm was centralised in _on_timer's finally, so the
    # old `_run_due_jobs` re-arm was removed (no double-schedule).
    assert armed["n"] == 1
