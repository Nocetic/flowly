"""Warm Codex subprocess lifecycle on a live policy reload.

A ``codex.policy.set`` can fire while a Flowly session is mid-turn. Because
``CodexSessionTool.execute`` holds its own local ``CodexSession`` reference for
the duration of ``run_turn``, the reload must NOT close the active session's
subprocess (that would break the in-flight turn) — it only drops it from the
warm pool so the NEXT turn respawns with the new config. Idle sessions are
closed immediately.
"""

from __future__ import annotations

import pytest

from flowly.agent.loop import AgentLoop


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeLoop:
    def __init__(self, sessions: dict) -> None:
        self._codex_sessions = sessions


@pytest.mark.asyncio
async def test_close_all_when_no_exclude():
    a, b = _FakeSession(), _FakeSession()
    fake = _FakeLoop({"s1": a, "s2": b})
    await AgentLoop._close_warm_codex_sessions(fake)
    assert a.closed and b.closed
    assert fake._codex_sessions == {}


@pytest.mark.asyncio
async def test_exclude_key_is_dropped_but_not_closed():
    active, other = _FakeSession(), _FakeSession()
    fake = _FakeLoop({"active": active, "other": other})
    await AgentLoop._close_warm_codex_sessions(fake, exclude_key="active")
    # The active turn's subprocess is left running…
    assert active.closed is False
    # …while every other warm session is closed.
    assert other.closed is True
    # Both are gone from the pool so the next turn respawns fresh.
    assert fake._codex_sessions == {}


@pytest.mark.asyncio
async def test_empty_pool_is_a_noop():
    fake = _FakeLoop({})
    await AgentLoop._close_warm_codex_sessions(fake, exclude_key="active")
    assert fake._codex_sessions == {}


@pytest.mark.asyncio
async def test_reload_reads_config_excludes_active_and_resyncs(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    calls: list = []

    class _Fake:
        _codex_active_session_key = "active-sk"
        _main_config = None

        async def _close_warm_codex_sessions(self, *, exclude_key=None):
            calls.append(("close", exclude_key))

        def sync_codex_session_tool(self):
            calls.append(("sync",))
            return True

    fake = _Fake()
    status = await AgentLoop.reload_codex_session_config(fake)

    assert status["ok"] is True
    assert status["registered"] is True
    # Config was (re)loaded onto the loop.
    assert fake._main_config is not None
    # The in-flight session is excluded from the close, and we re-register after.
    assert calls == [("close", "active-sk"), ("sync",)]
    # Status carries the applied policy for the RPC caller.
    assert "sandbox" in status and "approvalPolicy" in status
