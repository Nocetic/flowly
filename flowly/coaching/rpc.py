"""Meeting Coach RPCs (``coaching.*``) for every transport that carries them.

The direct gateway serves them per desktop WebSocket; the relay and the Flowly
server channel serve them per client session (one socket carries every
device). Both keep the same params and replies, so one implementation lives
here and each transport supplies only who asked (``owner``) and how to reach
that owner with an event (``emit``).

A Coach session belongs to the connection that last started it. Its events go
to that connection, and when the connection goes the session is stopped (and
finalized in the background). A transport whose clients come back under a new
id after a blip (the channel) gives a grace period, so the desktop's
``coaching.start`` from its new connection takes the session back before it is
stopped.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

METHODS = frozenset({
    "coaching.start",
    "coaching.segment",
    "coaching.askNow",
    "coaching.stop",
    "coaching.state",
    "coaching.snapshot",
    "coaching.update",
})

# Desktop ships PNG at the 1568 px Anthropic ceiling. UI screenshots compress
# to 200-500 KB raw → ~270-670 KB base64; 1.5 MB leaves headroom for busy,
# text-heavy screens without admitting malformed payloads. Over the cap the
# transcript still goes through, without the image.
MAX_SCREENSHOT_B64 = 1_500_000

#: ``emit(owner, event, data)`` sends one event to one connection.
Emit = Callable[[str, str, dict], Awaitable[None]]


class CoachingRpcError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CoachingRpc:
    def __init__(self, manager: Callable[[], Any], emit: Emit) -> None:
        self._manager = manager
        self._emit = emit
        #: owner → the Coach sessions it started.
        self.owned: dict[str, set[str]] = {}
        self._owner: dict[str, str] = {}
        self._callbacks: dict[str, tuple] = {}
        self._releases: set[asyncio.Task] = set()

    # ── ownership ─────────────────────────────────────────────────────────

    @staticmethod
    def session_id(owner: str, params: dict) -> str:
        """The client's ``sessionId``, else one per connection."""
        override = params.get("sessionId") or params.get("session_id")
        if override and isinstance(override, str):
            return override
        return f"coaching:{owner}"

    def track(self, owner: str, session_id: str) -> None:
        previous = self._owner.get(session_id)
        if previous is not None and previous != owner:
            self._discard(previous, session_id)
        self.owned.setdefault(owner, set()).add(session_id)
        self._owner[session_id] = owner

    def untrack(self, owner: str, session_id: str) -> None:
        self._discard(owner, session_id)
        if self._owner.get(session_id) == owner:
            self._owner.pop(session_id, None)
            self._callbacks.pop(session_id, None)

    def _discard(self, owner: str, session_id: str) -> None:
        owned = self.owned.get(owner)
        if not owned:
            return
        owned.discard(session_id)
        if not owned:
            self.owned.pop(owner, None)

    async def release(self, owner: str, grace_seconds: float = 0.0) -> None:
        """The owner's connection went: stop its sessions, now or after a grace period."""
        owned = self.owned.pop(owner, set())
        # A session started before ownership tracking, or by an older caller
        # using the derived id.
        owned.add(f"coaching:{owner}")
        if grace_seconds <= 0:
            await self._stop_released(owner, owned)
            return
        task = asyncio.create_task(self._stop_after(owner, owned, grace_seconds), name="coaching-release")
        self._releases.add(task)
        task.add_done_callback(self._releases.discard)

    async def _stop_after(self, owner: str, sessions: set[str], grace_seconds: float) -> None:
        await asyncio.sleep(grace_seconds)
        await self._stop_released(owner, sessions)

    async def _stop_released(self, owner: str, sessions: set[str]) -> None:
        manager = self._manager()
        for session_id in sessions:
            if self._owner.get(session_id, owner) != owner:
                continue  # another connection took it back meanwhile
            self._owner.pop(session_id, None)
            self._callbacks.pop(session_id, None)
            if manager is None or not manager.is_active(session_id):
                continue
            try:
                await manager.stop(session_id, background_finalize=True)
                logger.info(f"[Coach] auto-stopped session {session_id}: its connection went")
            except Exception as e:
                logger.warning(f"[Coach] auto-stop failed for {session_id}: {e}")

    # ── events ────────────────────────────────────────────────────────────

    def _wire(self, manager: Any, session_id: str) -> None:
        """One set of callbacks per session; each reaches whoever owns it now."""
        callbacks = self._callbacks.get(session_id)
        if callbacks is None:
            async def send(event: str, data: dict) -> None:
                owner = self._owner.get(session_id)
                if owner is None:
                    return
                try:
                    await self._emit(owner, event, data)
                except Exception as e:
                    logger.debug(f"[Coach] {event} for {session_id} not delivered: {e}")

            async def tip(sid: str, tip_text: str, confidence: float, *, seq: int = -1,
                          timestamp: float | None = None) -> None:
                await send("coaching.tip", {
                    "sessionId": sid, "text": tip_text, "confidence": confidence,
                    "timestamp": timestamp if timestamp is not None else time.time(), "seq": seq,
                })

            async def transcript(sid: str, text: str, source: str, *, seq: int = -1) -> None:
                await send("coaching.transcript", {"sessionId": sid, "text": text, "source": source, "seq": seq})

            async def finalized(sid: str, summary: dict) -> None:
                await send("coaching.finalized", {"sessionId": sid, **summary})

            async def gate_decision(sid: str, **payload) -> None:
                # The desktop's Diagnostics panel; best-effort.
                await send("coaching.gate_decision", {"sessionId": sid, "timestamp": time.time(), **payload})

            callbacks = self._callbacks[session_id] = (tip, transcript, finalized, gate_decision)
        tip, transcript, finalized, gate_decision = callbacks
        # The manager keeps each callback once; a restarted session is a new
        # session object and gets them again.
        manager.on_tip(session_id, tip)
        manager.on_transcript(session_id, transcript)
        manager.on_finalized(session_id, finalized)
        manager.on_gate_decision(session_id, gate_decision)

    # ── methods ───────────────────────────────────────────────────────────

    async def call(self, owner: str, method: str, params: dict) -> dict:
        """Run one ``coaching.*`` method. Raises :class:`CoachingRpcError`."""
        manager = self._manager()
        session_id = self.session_id(owner, params)
        if manager is None:
            if method in ("coaching.state", "coaching.snapshot"):
                return {"active": False}
            raise CoachingRpcError("NOT_AVAILABLE", "Coaching not configured")

        if method == "coaching.start":
            # Start (or reconfigure) the session BEFORE registering callbacks,
            # so registration targets the now-existing session.
            result = await manager.start(
                session_id=session_id,
                user_context=str(params.get("context", "") or ""),
                language=str(params.get("language", "auto") or "auto"),
                frequency=str(params.get("frequency", "moderate") or "moderate"),
            )
            if result.get("status") == "at_capacity":
                raise CoachingRpcError("CAPACITY", f"Max {result.get('limit')} concurrent coaching sessions")
            self.track(owner, session_id)
            self._wire(manager, session_id)
            result["sessionId"] = session_id
            return result

        if method == "coaching.segment":
            # The transcript is made on the desktop; Core only sees text.
            # "text" is current, "transcript" legacy.
            text = params.get("text") or params.get("transcript") or ""
            if not text or not isinstance(text, str):
                raise CoachingRpcError("INVALID_REQUEST", "text is required")
            source = str(params.get("source", "mic") or "mic")
            if source not in ("mic", "system"):
                source = "mic"
            screenshot = _screenshot(params, session_id, "segment")
            logger.info(
                f"[Coach] segment.rpc session={session_id} text_len={len(text)} source={source} "
                f"screenshot={'present(' + str(len(screenshot)) + 'b)' if screenshot else 'none'}"
            )
            return await manager.add_transcript(
                session_id=session_id, text=text, source=source, screenshot_b64=screenshot,
            )

        if method == "coaching.askNow":
            # The hotkey "force a tip": skips gate 1, runs gate 2 on the
            # cached transcript and the (optional) fresh screenshot.
            screenshot = _screenshot(params, session_id, "askNow")
            logger.info(
                f"[Coach] askNow.rpc session={session_id} "
                f"screenshot={'present(' + str(len(screenshot)) + 'b)' if screenshot else 'none'}"
            )
            return await manager.ask_now(session_id=session_id, screenshot_b64=screenshot)

        if method == "coaching.stop":
            result = await manager.stop(session_id)
            # After the stop, so a finalized event raised by it still has a
            # route. Whoever holds it: a reconnected desktop may stop a session
            # its earlier connection started.
            self.untrack(self._owner.get(session_id, owner), session_id)
            result["sessionId"] = session_id
            return result

        if method == "coaching.state":
            info = manager.session_info(session_id)
            return {"active": True, **info} if info else {"active": False}

        if method == "coaching.snapshot":
            # The buffered transcript and tips, for rebuilding the live view
            # after a reconnect or a page change (`state` has counts only).
            snapshot = manager.session_snapshot(session_id)
            return {"active": True, **snapshot} if snapshot else {"active": False, "session_id": session_id}

        if method == "coaching.update":
            # Live changes to a running session (context, frequency, language).
            context, frequency, language = params.get("context"), params.get("frequency"), params.get("language")
            return await manager.update_session(
                session_id,
                user_context=context if isinstance(context, str) else None,
                frequency=frequency if isinstance(frequency, str) else None,
                language=language if isinstance(language, str) else None,
            )

        raise CoachingRpcError("METHOD_NOT_FOUND", f"unknown coaching method: {method}")


def _screenshot(params: dict, session_id: str, label: str) -> str | None:
    raw = params.get("screenshot")
    screenshot = raw if isinstance(raw, str) else None
    if screenshot is not None and len(screenshot) > MAX_SCREENSHOT_B64:
        logger.warning(
            f"[Coach] {label} session={session_id} screenshot too large ({len(screenshot)} bytes); dropping image"
        )
        return None
    return screenshot
