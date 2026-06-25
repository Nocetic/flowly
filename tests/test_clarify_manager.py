"""ClarifyManager pauses the agent on an asyncio.Future until a surface
answers, mirroring the exec approval manager but resolving to free text."""

from __future__ import annotations

import asyncio
import time

import pytest

from flowly.clarify.manager import ClarifyManager
from flowly.clarify.types import ClarifyRequest


def _pending(timeout: float = 5.0, **kw) -> ClarifyRequest:
    now = time.time()
    return ClarifyRequest(
        id=kw.pop("id", "abc123"),
        question=kw.pop("question", "Which one?"),
        choices=kw.pop("choices", ["A", "B"]),
        session_key=kw.pop("session_key", "web:1"),
        created_at=now,
        expires_at=now + timeout,
    )


@pytest.mark.asyncio
async def test_resolve_returns_answer():
    mgr = ClarifyManager()
    pending = _pending()

    async def answer_soon():
        # Let request_and_wait register the future first.
        await asyncio.sleep(0.01)
        assert mgr.resolve(pending.id, "B")

    asyncio.create_task(answer_soon())
    result = await mgr.request_and_wait(pending)
    assert result == "B"


@pytest.mark.asyncio
async def test_notify_callbacks_fire():
    mgr = ClarifyManager()
    seen = []
    mgr.add_notify_callback(lambda p: seen.append(p.id) or _noop())

    pending = _pending()

    async def answer_soon():
        await asyncio.sleep(0.01)
        mgr.resolve(pending.id, "A")

    asyncio.create_task(answer_soon())
    await mgr.request_and_wait(pending)
    assert seen == [pending.id]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_timeout_returns_none():
    mgr = ClarifyManager()
    pending = _pending(timeout=0.05)
    result = await mgr.request_and_wait(pending)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_unknown_id_is_false():
    mgr = ClarifyManager()
    assert mgr.resolve("nope", "x") is False


@pytest.mark.asyncio
async def test_double_resolve_is_false():
    mgr = ClarifyManager()
    pending = _pending()

    async def answer():
        await asyncio.sleep(0.01)
        assert mgr.resolve(pending.id, "A") is True
        # Future already done — second resolve is a no-op.
        assert mgr.resolve(pending.id, "B") is False

    asyncio.create_task(answer())
    result = await mgr.request_and_wait(pending)
    assert result == "A"


@pytest.mark.asyncio
async def test_list_pending_tracks_inflight():
    mgr = ClarifyManager()
    pending = _pending()

    async def check_then_answer():
        await asyncio.sleep(0.01)
        ids = [p.id for p in mgr.list_pending()]
        assert pending.id in ids
        mgr.resolve(pending.id, "A")

    asyncio.create_task(check_then_answer())
    await mgr.request_and_wait(pending)
    # Cleaned up after resolution.
    assert mgr.list_pending() == []


@pytest.mark.asyncio
async def test_cron_context_short_circuits(monkeypatch):
    import flowly.clarify.manager as mod
    monkeypatch.setattr(mod.ClarifyManager, "_in_cron_context", staticmethod(lambda: True))
    mgr = ClarifyManager()
    # Should return immediately without registering a future.
    result = await mgr.request_and_wait(_pending())
    assert result is None
    assert mgr.list_pending() == []
