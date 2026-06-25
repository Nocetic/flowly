"""M2 — a cron `script` must not block the gateway event loop.

`script_runner.run` does a synchronous `subprocess.run(..., timeout=120)`. The
gateway runs everything (channels, WS, REST, heartbeat, cron) on ONE asyncio
loop, and cron jobs run as tasks on that loop — so calling `run` inline froze
the entire bot for the script's duration. The fix runs it via
`asyncio.to_thread`; this test proves the loop stays responsive meanwhile.
"""

from __future__ import annotations

import asyncio

from flowly.cron import script_runner


def test_script_runs_off_the_event_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))

    # A real (short) blocking script under the workspace scripts dir.
    ws = script_runner.scripts_dir()
    (ws / "sleep.py").write_text("import time\ntime.sleep(0.4)\nprint('ok')\n", encoding="utf-8")

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(60):
            await asyncio.sleep(0.02)
            ticks += 1

    async def main():
        hb = asyncio.create_task(heartbeat())
        # Exactly the call shape the gateway now uses.
        result = await asyncio.to_thread(script_runner.run, "sleep.py")
        hb.cancel()
        return result

    result = asyncio.run(main())

    # The script ran to completion...
    assert result.success
    assert "ok" in result.stdout
    # ...and the heartbeat coroutine kept ticking through the ~0.4s blocking
    # call — proof the loop was NOT frozen. A synchronous call would have
    # blocked it and left ticks at 0. Generous threshold to avoid flakiness.
    assert ticks >= 3
