"""Board push notifications."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from flowly.push.board_push import notify_board_finished


@dataclass
class _Card:
    id: str = "c_1"
    title: str = "Ship task"
    result: str = "first line\nsecond line"
    error: str = ""


@pytest.mark.asyncio
async def test_board_finished_push_payload(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    await notify_board_finished(_Card(), "done")

    assert calls == [{
        "title": "Board · Ship task",
        "body": "first line",
        "data": {
            "type": "board",
            "cardId": "c_1",
            "outcome": "done",
        },
    }]


@pytest.mark.asyncio
async def test_board_failed_push_payload(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    await notify_board_finished(_Card(error="boom"), "failed")

    assert calls[0]["title"] == "Board · Ship task"
    assert calls[0]["body"] == "failed: boom"
    assert calls[0]["data"]["type"] == "board"
    assert calls[0]["data"]["outcome"] == "failed"
