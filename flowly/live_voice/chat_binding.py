"""A Live Voice call that belongs to the chat it starts in (docs/chat-wire-protocol.md §7).

The call's own session (``desktop:voice:<conversationId>``) stays its ledger:
connections, transcript revisions, tools. A bound call adds three things to
its chat (``web:<id>``, the main agent's):

- a record, ``chat {state:"voice_call"}``, on every open and end. It is a
  shared conversation event (never private, never with a ``runId``), so the
  Flowly server stores it as the chat's one ``voice_call`` row;
- one hidden context row (``kind: voice_call_context``) through which the
  text agent knows what was said. It is never written into a chat while a
  turn is running there: a turn saves its session with compare-and-swap, and
  a write in between would make that save fail and the turn be lost. It goes
  either into a starting turn's own session object (``on_turn_start``,
  saved with the turn) or under the chat's turn lock after the call
  (``finish``);
- for ``chat.send``, ``origin: "voice"`` with the call's id on the user row
  of a request the voice made in the chat (``chat_send_origin``).
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from flowly.live_voice.sessions import VoiceError, identity, session_key as voice_session_key
from flowly.session.manager import Session, SessionManager

#: ``system.capabilities.voice.chatBindingVersion``.
CHAT_BINDING_VERSION = 1
CHAT_KEY = re.compile(r"^web:[A-Za-z0-9_-]{1,128}$")
CONTEXT_KIND = "voice_call_context"
MAX_TURNS = 400
MAX_TURN_CHARS = 2000
MAX_TURNS_BYTES = 200 * 1024
#: A bound call whose last connection never ended is over after this much silence.
ABANDONED_SECONDS = 600
#: The context row's own cap: the newest words of a long call.
MAX_CONTEXT_CHARS = 24_000


def chat_key(value: Any, *, profile: str) -> str:
    """The chat a call may bind to: one of the main agent's ``web:`` chats (v1)."""
    if not isinstance(value, str) or not CHAT_KEY.fullmatch(value):
        raise VoiceError("INVALID_PARAMS", "chatSessionKey must be one of the main agent's chats (web:<id>).")
    if profile != "default":
        raise VoiceError("INVALID_PARAMS", "A call joins a chat only with the main agent.")
    return value


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    # ``updatedAt`` is written as naive local time (``datetime.now()``); the
    # connection and utterance times are UTC with an offset.
    return moment if moment.tzinfo else moment.astimezone(timezone.utc)


def _wire_time(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def transcript(session: Session) -> tuple[list[dict], bool]:
    """Each utterance's final text, in order; the newest kept within the limits."""
    turns: list[dict] = []
    truncated = False
    for message in session.messages:
        if message.get("kind") != "voice" or message.get("role") not in {"user", "assistant"}:
            continue
        voice = message.get("voice") if isinstance(message.get("voice"), dict) else {}
        text = str(message.get("content") or "").strip()
        interrupted = voice.get("delivery") == "interrupted"
        if not text and not interrupted:
            continue
        if len(text) > MAX_TURN_CHARS:
            text, truncated = text[:MAX_TURN_CHARS], True
        turn = {"role": message["role"], "text": text,
                "at": _wire_time(_utc(voice.get("createdAt")) or _utc(message.get("timestamp")))}
        if turn["at"] is None:
            continue
        if interrupted:
            turn["interrupted"] = True
        turns.append(turn)
    if len(turns) > MAX_TURNS:
        turns, truncated = turns[-MAX_TURNS:], True
    while turns and len(json.dumps(turns, ensure_ascii=False).encode()) > MAX_TURNS_BYTES:
        turns, truncated = turns[1:], True
    return turns, truncated


def call_record(session: Session, *, now: datetime | None = None) -> dict | None:
    """The record for a bound call as its ledger now stands; None for an unbound one."""
    voice = session.metadata.get("voice") if isinstance(session.metadata.get("voice"), dict) else {}
    chat = voice.get("chatSessionKey")
    connections = [c for c in voice.get("connections") or [] if isinstance(c, dict)]
    if not isinstance(chat, str) or not connections:
        return None
    now = now or datetime.now(timezone.utc)
    duration = 0.0
    for connection in connections:
        opened = _utc(connection.get("openedAt"))
        closed = _utc(connection.get("endedAt")) or now
        if opened is not None and closed > opened:
            duration += (closed - opened).total_seconds()
    ended = all(c.get("endedAt") for c in connections)
    turns, truncated = transcript(session) if ended else ([], False)
    # An ended call where nobody spoke takes its live record back (§7.3).
    phase = ("ended" if turns else "discarded") if ended else "live"
    record: dict[str, Any] = {
        "state": "voice_call", "sessionKey": chat, "callId": session.metadata.get("voiceConversationId"),
        "phase": phase,
        "startedAt": _wire_time(_utc(connections[0].get("openedAt"))),
        "endedAt": _wire_time(_utc(connections[-1].get("endedAt"))) if ended else None,
        "durationMs": int(duration * 1000),
    }
    if phase == "ended":
        record["turns"], record["truncated"] = turns, truncated
    return record


def context_text(record: dict, turns: list[dict]) -> str:
    """What the text agent reads about a call in its chat (the user's words are untrusted data)."""
    seconds = record["durationMs"] // 1000
    head = (f"[Voice call in this chat, started {record['startedAt']}, {seconds // 60}:{seconds % 60:02d}"
            f"{', still going' if record['phase'] == 'live' else ''}. The transcript of what was said:]")
    lines = [f"{'User' if turn['role'] == 'user' else 'Agent'}: {turn['text'] or '(interrupted)'}" for turn in turns]
    body = "\n".join(lines)
    if len(body) > MAX_CONTEXT_CHARS:
        body = "…\n" + body[-MAX_CONTEXT_CHARS:]
    return f"{head}\n{body}" if body else f"{head}\n(nothing was said yet)"


def upsert_context(session: Session, call_id: str, text: str | None) -> bool:
    """Put the call's hidden context row into this chat session, in place when it is there.

    ``text`` None removes it: a call where nobody spoke leaves nothing.
    """
    for index, message in enumerate(session.messages):
        if message.get("kind") == CONTEXT_KIND and message.get("voiceCallId") == call_id:
            if text is None:
                del session.messages[index]
                return True
            if message.get("content") == text:
                return False
            message["content"] = text
            return True
    if text is None:
        return False
    session.add_message("user", text, kind=CONTEXT_KIND, voiceCallId=call_id, _display_hidden=True)
    return True


def _has_context(session: Session | None, call_id: str) -> bool:
    return bool(session) and any(m.get("kind") == CONTEXT_KIND and m.get("voiceCallId") == call_id
                                 for m in session.messages)


class VoiceChatBindings:
    """The primary runtime's bound calls: which call belongs to which chat, and its record.

    ``emit`` sends a record as a shared chat event; ``between_turns`` applies a
    session operation under the chat's turn lock (``AgentLoop.mutate_between_turns``).
    """

    def __init__(self, sessions: SessionManager, *,
                 emit: Callable[[dict], Awaitable[None]] | None = None,
                 between_turns: Callable[[str, Callable[[Session], Any]], Awaitable[Any]] | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.sessions = sessions
        self.emit = emit
        self.between_turns = between_turns
        self.now = now
        self._chats: dict[str, set[str]] = {}
        self._calls: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        # Records replace each other on the server (the last wins), so they go
        # out one at a time, in the order the call changed.
        self._sending: asyncio.Lock | None = None

    # ── which call belongs where ──────────────────────────────────────────

    def bind(self, call_id: str, chat: str) -> None:
        self._calls[call_id] = chat
        self._chats.setdefault(chat, set()).add(call_id)

    def chat_of(self, call_id: str) -> str | None:
        return self._calls.get(call_id)

    def rebuild(self) -> None:
        """Learn every bound call from the ledgers (startup; trusted in-process scope)."""
        for row in self.sessions.list_sessions():
            if row.get("kind") != "voice" or not isinstance(row.get("voiceConversationId"), str):
                continue
            session = self.sessions.read(voice_session_key(row["voiceConversationId"]))
            voice = (session.metadata.get("voice") or {}) if session else {}
            chat = voice.get("chatSessionKey")
            if isinstance(chat, str) and CHAT_KEY.fullmatch(chat) and not voice.get("deletedAt"):
                self.bind(row["voiceConversationId"], chat)

    def chat_send_origin(self, params: dict, session: str) -> dict:
        """The user-row fields of a voice request in a chat; ``{}`` for an ordinary message.

        Raises ``ValueError`` for an origin Core does not know or a call that
        does not belong to this chat.
        """
        if params.get("origin") is None:
            return {}
        fields = _origin_fields(params)
        if self.chat_of(fields["voiceCallId"]) != session:
            raise ValueError("voiceCallId is not a call of this chat.")
        return fields

    # ── the record and the chat ───────────────────────────────────────────

    def changed(self, call_id: str) -> None:
        """A bound call was opened or ended: send its record as it is now; after an end, write its chat context."""
        if self.chat_of(call_id) is None:
            return
        session = self.sessions.read(voice_session_key(call_id))
        record = call_record(session, now=self.now()) if session else None
        if record is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._publish(call_id, record))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish(self, call_id: str, record: dict | None = None) -> None:
        from flowly.live_voice.authority import request_owner_scope
        from flowly.live_voice.events import event_access_scope

        # The record belongs to the chat, not to the request that ended the
        # call: shared, so the Flowly server may store it (a private frame
        # is never stored) and every viewer of the chat gets it.
        if self._sending is None:
            self._sending = asyncio.Lock()
        async with self._sending:
            await self._send(call_id, record)

    async def _send(self, call_id: str, record: dict | None) -> None:
        from flowly.live_voice.authority import request_owner_scope
        from flowly.live_voice.events import event_access_scope

        with request_owner_scope(None), event_access_scope(None):
            if record is None:
                session = self.sessions.read(voice_session_key(call_id))
                record = call_record(session, now=self.now()) if session else None
            if record is None:
                return
            if self.emit is not None:
                try:
                    await self.emit(record)
                except Exception as exc:  # noqa: BLE001 — the sweep sends the end again
                    logger.warning("[voice] call record not sent: {}", type(exc).__name__)
            if record["phase"] != "live":
                await self.finish(call_id)

    async def finish(self, call_id: str) -> bool:
        """Write an ended call's context into its chat between turns; remember how far it got."""
        chat = self.chat_of(call_id)
        session = self.sessions.read(voice_session_key(call_id))
        if chat is None or session is None or self.between_turns is None:
            return False
        record = call_record(session, now=self.now())
        if record is None or record["phase"] == "live":
            return False
        revision = session.metadata["voice"].get("revision", 0)
        # Nobody spoke (discarded): no row, and none is created for a chat Core has never seen.
        text = context_text(record, record["turns"]) if record["phase"] == "ended" else None
        try:
            if text is not None or _has_context(self.sessions.read(chat), call_id):
                await self.between_turns(chat, lambda chat_session: upsert_context(chat_session, call_id, text))
        except Exception as exc:  # noqa: BLE001 — the sweep tries again
            logger.warning("[voice] call context not written to its chat: {}", type(exc).__name__)
            return False

        def mark(ledger: Session) -> None:
            ledger.metadata["voice"]["chatMirror"] = {"revision": revision}

        self.sessions.mutate(voice_session_key(call_id), mark)
        return True

    def on_turn_start(self, session: Session) -> None:
        """A turn starts in a chat: put its live calls into this turn's own session object.

        The ledger is read in the trusted scope: the turn may run as the host
        (a typed message over the Flowly server) while the call is an
        account's, and the call's words belong to this chat because its owner
        bound it here (``voice.open`` checked that chat). Only calls bound to
        this very chat are read.
        """
        from flowly.live_voice.authority import request_owner_scope

        with request_owner_scope(None):
            for call_id in sorted(self._chats.get(session.key, ())):
                ledger = self.sessions.read(voice_session_key(call_id))
                record = call_record(ledger, now=self.now()) if ledger else None
                if record is None or record["phase"] != "live":
                    continue  # an ended call's context is written by finish()
                turns, _ = transcript(ledger)
                if turns:
                    upsert_context(session, call_id, context_text(record, turns))

    async def sweep(self) -> None:
        """End abandoned bound calls; finish any chat context an end left unwritten."""
        from flowly.live_voice.authority import request_owner_scope

        with request_owner_scope(None):
            for call_id in list(self._calls):
                key = voice_session_key(call_id)
                ledger = self.sessions.read(key)
                if ledger is None:
                    continue
                voice = ledger.metadata.get("voice") or {}
                connections = voice.get("connections") or []
                if connections and not connections[-1].get("endedAt"):
                    last = _utc(voice.get("updatedAt")) or _utc(connections[-1].get("openedAt"))
                    if last is None or (self.now() - last).total_seconds() < ABANDONED_SECONDS:
                        continue
                    stamp = voice.get("updatedAt") or connections[-1]["openedAt"]

                    def close(session: Session, stamp: str = stamp) -> None:
                        current = session.metadata["voice"]["connections"][-1]
                        if not current.get("endedAt"):
                            current["endedAt"] = stamp
                            session.metadata["voice"]["revision"] += 1

                    self.sessions.mutate(key, close)
                    logger.info("[voice] an abandoned call was ended ({} silent)", call_id[:8])
                    await self._publish(call_id)
                    continue
                mirrored = (voice.get("chatMirror") or {}).get("revision", -1)
                if connections and voice.get("revision", 0) > mirrored:
                    await self._publish(call_id)

    async def wait(self) -> None:
        """Wait for records being sent (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


def _origin_fields(params: dict) -> dict:
    origin = params.get("origin")
    if origin != "voice":
        raise ValueError("origin must be \"voice\".")
    call_id = params.get("voiceCallId")
    try:
        identity(call_id, "voiceCallId")
    except VoiceError as exc:
        raise ValueError(str(exc)) from None
    return {"origin": "voice", "voiceCallId": call_id}


def host_assigned_origin(params: dict) -> dict:
    """A named agent's ``chat.send`` from a call (§7.5): the primary host checks the call, then assigns.

    Pops the client's fields; returns the ones the host vouches for (``{}``
    without an origin). Raises ``ValueError`` when the call is not a bound
    call of this host.
    """
    if params.get("origin") is None:
        params.pop("voiceCallId", None)
        return {}
    fields = _origin_fields(params)
    params.pop("origin", None)
    params.pop("voiceCallId", None)
    bindings = active_bindings()
    if bindings is None or bindings.chat_of(fields["voiceCallId"]) is None:
        raise ValueError("voiceCallId is not a call of this agent host.")
    return fields


def runtime_chat_origin(params: dict, session: str, *, owns_voice: bool) -> dict:
    """``chat.send``'s ``origin`` on a runtime's own gateway.

    The primary runtime knows its calls: the call must belong to this chat.
    A named agent's runtime is reached only through the primary host, which
    checked the call and assigned the fields (``host_assigned_origin``).
    """
    if params.get("origin") is None:
        return {}
    if not owns_voice:
        return _origin_fields(params)
    bindings = active_bindings()
    if bindings is None:
        raise ValueError("Voice calls cannot join a chat on this agent.")
    return bindings.chat_send_origin(params, session)


_active: VoiceChatBindings | None = None


def set_active_bindings(bindings: VoiceChatBindings | None) -> None:
    """The primary runtime's bindings, for the chat transports (``chat.send`` ``origin``)."""
    global _active
    _active = bindings


def active_bindings() -> VoiceChatBindings | None:
    return _active


def public_binding(session: Session) -> dict:
    voice = session.metadata.get("voice") or {}
    chat = voice.get("chatSessionKey")
    return {"chatSessionKey": chat} if isinstance(chat, str) else {}


__all__ = [
    "ABANDONED_SECONDS", "CHAT_BINDING_VERSION", "CONTEXT_KIND", "VoiceChatBindings", "call_record", "chat_key",
    "active_bindings", "context_text", "host_assigned_origin", "public_binding", "runtime_chat_origin",
    "set_active_bindings", "transcript", "upsert_context",
]
