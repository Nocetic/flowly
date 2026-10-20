"""Bounded, connection-owned screen status subscriptions (never pixel transport)."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from flowly.desktop_runtime.authority import ScreenError


@dataclass
class Subscription:
    identifier: str
    ws: Any
    client_id: str
    target: dict
    snapshot: dict
    task: asyncio.Task | None = None


class DisplaySubscriptions:
    INTERVAL = 1.0
    HEARTBEAT_INTERVAL = 15.0
    MAX_TOTAL = 16
    MAX_PER_CONNECTION = 4

    def __init__(self, gateway, resolve: Callable):
        self.gateway, self.resolve = gateway, resolve
        self.entries: dict[str, Subscription] = {}
        self._admission = asyncio.Lock()
        self._closed = False

    def current(self, subscription: Subscription) -> bool:
        return self.gateway.display_client_authenticated(subscription.client_id, subscription.ws)

    def sample(self, target: dict) -> dict:
        return self.resolve(self.gateway, target, pinned=True).status()

    async def prepare(self, ws, client_id: str, target: dict) -> Subscription:
        # Admission includes the pending snapshot, so concurrent subscribes
        # cannot all pass the capacity check before any record is registered.
        async with self._admission:
            if self._closed:
                raise ScreenError("CONTROL_UNAVAILABLE")
            count = sum(entry.ws is ws for entry in self.entries.values())
            if len(self.entries) >= self.MAX_TOTAL or count >= self.MAX_PER_CONNECTION:
                raise ScreenError("SCREEN_BUSY")
            snapshot = await asyncio.to_thread(self.sample, target)
            captured = {"profileId": snapshot["profileId"], "expectedHostId": snapshot["hostId"],
                        "expectedBotId": snapshot["botId"]}
            entry = Subscription(secrets.token_urlsafe(24), ws, client_id, captured, snapshot)
            if self._closed or not self.current(entry):
                raise ScreenError("UNAUTHORIZED")
            self.entries[entry.identifier] = entry
            return entry

    def activate(self, entry: Subscription) -> None:
        # Called after the RPC snapshot reply: the first event cannot overtake it.
        if not self._closed and self.entries.get(entry.identifier) is entry and entry.task is None:
            entry.task = asyncio.create_task(self._follow(entry), name="screen-status-subscription")

    async def remove(self, ws, identifier: str) -> None:
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ScreenError("INVALID_PARAMS")
        entry = self.entries.get(identifier)
        if entry is None:
            return  # An already removed subscription has no remaining authority.
        if entry.ws is not ws:
            raise ScreenError("UNAUTHORIZED")
        self.entries.pop(identifier)
        if entry.task is not None:
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)

    async def _send(self, entry: Subscription, event: str, data: dict) -> None:
        if self._closed or self.entries.get(entry.identifier) is not entry or not self.current(entry):
            raise ScreenError("UNAUTHORIZED")
        await asyncio.wait_for(entry.ws.send_json({"type": "event", "event": event,
            "data": {"subscriptionId": entry.identifier, **data}}), 2)

    async def _follow(self, entry: Subscription) -> None:
        last_sent = time.monotonic()
        try:
            while self.current(entry):
                await asyncio.sleep(self.INTERVAL)
                if not self.current(entry):
                    break
                snapshot = await asyncio.to_thread(self.sample, entry.target)
                if snapshot["statusRevision"] < entry.snapshot["statusRevision"]:
                    raise ScreenError("STATUS_STATE_INVALID")
                if snapshot["statusRevision"] == entry.snapshot["statusRevision"]:
                    if time.monotonic() - last_sent >= self.HEARTBEAT_INTERVAL:
                        identity = {key: snapshot[key] for key in ("hostId", "botId", "profileId", "screenId", "generation", "statusRevision")}
                        await self._send(entry, "display.heartbeat", identity)
                        last_sent = time.monotonic()
                    continue
                previous = entry.snapshot
                await self._send(entry, "display.status", snapshot)
                if snapshot.get("control") != previous.get("control"):
                    await self._send(entry, "display.control", snapshot)
                if snapshot["phase"] == "failed" and previous["phase"] != "failed":
                    await self._send(entry, "display.runtime.failed", snapshot)
                entry.snapshot = snapshot
                last_sent = time.monotonic()
        except asyncio.TimeoutError:
            pass  # A slow consumer loses only its subscription, never control.
        except Exception as exc:
            if self.current(entry):
                # No paths, endpoint details or arbitrary exception strings.
                code = exc.code if isinstance(exc, ScreenError) else "CONTROL_UNAVAILABLE"
                try:
                    identity = {key: entry.snapshot[key] for key in ("hostId", "botId", "profileId", "screenId")}
                    await self._send(entry, "display.subscription.closed", {**identity,
                        "generation": entry.snapshot["generation"], "statusRevision": entry.snapshot["statusRevision"],
                        "code": code})
                except (OSError, asyncio.TimeoutError, ScreenError):
                    pass
        finally:
            if self.entries.get(entry.identifier) is entry:
                self.entries.pop(entry.identifier)

    async def close(self) -> None:
        self._closed = True
        entries = tuple(self.entries.values())
        self.entries.clear()
        tasks = [entry.task for entry in entries if entry.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
