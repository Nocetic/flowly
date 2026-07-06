"""Exec-approval push notifications."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from flowly.push.approval_push import notify_approval_requested


@dataclass
class _Request:
    command: str = "rm -rf ./build"


@dataclass
class _Pending:
    id: str = "a_1"
    request: _Request = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.request is None:
            self.request = _Request()


@pytest.mark.asyncio
async def test_approval_push_payload(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    await notify_approval_requested(_Pending())

    assert calls == [{
        "title": "Approval required",
        "body": "rm -rf ./build",
        "data": {
            "type": "approval",
            "id": "a_1",
        },
    }]


@pytest.mark.asyncio
async def test_approval_push_empty_command_falls_back(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    await notify_approval_requested(_Pending(request=_Request(command="")))

    assert calls[0]["body"] == "A command needs your approval"
    assert calls[0]["data"]["id"] == "a_1"


@pytest.mark.asyncio
async def test_approval_push_never_raises(monkeypatch) -> None:
    async def boom(*args, **kwargs) -> None:
        raise RuntimeError("relay down")

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", boom)
    # Best-effort: a relay failure must not propagate into the approval wait.
    await notify_approval_requested(_Pending())
