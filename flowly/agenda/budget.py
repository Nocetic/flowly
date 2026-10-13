"""One daily budget for every agent on this machine.

Each agent runs in its own process with its own home, so the count of messages
the agents sent first today lives in the canonical home
(``~/.flowly/agenda/budget.json``), under a file lock. A wake-up reserves a
slot before its turn runs (so two agents waking together cannot both take the
last one), keeps it when the message goes out, and gives it back when the
agent decides to stay silent or the turn fails.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time
from pathlib import Path

from filelock import FileLock, Timeout

from flowly.profile import _atomic_write_json, default_home

#: A reservation older than this belongs to a turn that never came back.
STALE_RESERVATION_MS = 30 * 60 * 1000
#: Entries older than this are no longer part of any day's count.
KEEP_MS = 3 * 24 * 60 * 60 * 1000


class ProactiveBudget:
    def __init__(self, path: Path | None = None, *, lock_timeout: float = 10.0):
        self.path = Path(path) if path else default_home() / "agenda" / "budget.json"
        self._lock = FileLock(str(self.path.with_suffix(".lock")))
        self._lock_timeout = lock_timeout

    def _read(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = raw.get("entries") if isinstance(raw, dict) else None
        return [e for e in entries or [] if isinstance(e, dict) and isinstance(e.get("at"), int)]

    def _write(self, entries: list[dict]) -> None:
        _atomic_write_json(self.path, {"version": 1, "entries": entries})

    @staticmethod
    def _live(entries: list[dict], now_ms: int) -> list[dict]:
        return [
            e for e in entries
            if now_ms - e["at"] < KEEP_MS
            and not (e.get("state") == "reserved" and now_ms - e["at"] > STALE_RESERVATION_MS)
        ]

    @staticmethod
    def _today(entries: list[dict], now_ms: int, zone: dt.tzinfo) -> list[dict]:
        day = dt.datetime.fromtimestamp(now_ms / 1000, zone).date()
        return [e for e in entries if dt.datetime.fromtimestamp(e["at"] / 1000, zone).date() == day]

    def counts(self, profile: str, *, zone: dt.tzinfo, now_ms: int | None = None) -> tuple[int, int]:
        """(sent or reserved today by ``profile``, by every agent)."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        try:
            with self._lock.acquire(timeout=self._lock_timeout):
                today = self._today(self._live(self._read(), now), now, zone)
        except Timeout:
            return 0, 0
        return sum(1 for e in today if e.get("profile") == profile), len(today)

    def reserve(
        self, profile: str, *, zone: dt.tzinfo, agent_cap: int, global_cap: int, now_ms: int | None = None,
    ) -> str | None:
        """A slot for one message, or None when today's budget is spent."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock.acquire(timeout=self._lock_timeout):
                entries = self._live(self._read(), now)
                today = self._today(entries, now, zone)
                if len(today) >= global_cap or sum(1 for e in today if e.get("profile") == profile) >= agent_cap:
                    return None
                token = secrets.token_hex(8)
                entries.append({"token": token, "profile": profile, "at": now, "state": "reserved"})
                self._write(entries)
                return token
        except Timeout:
            return None

    def confirm(self, token: str) -> None:
        self._settle(token, keep=True)

    def release(self, token: str) -> None:
        self._settle(token, keep=False)

    def _settle(self, token: str, *, keep: bool) -> None:
        if not token:
            return
        try:
            with self._lock.acquire(timeout=self._lock_timeout):
                entries = self._read()
                if keep:
                    for entry in entries:
                        if entry.get("token") == token:
                            entry["state"] = "sent"
                else:
                    entries = [e for e in entries if e.get("token") != token]
                self._write(entries)
        except Timeout:
            pass
