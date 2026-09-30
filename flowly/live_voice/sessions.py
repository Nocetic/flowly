"""Canonical voice transcripts. Recording speech never submits an agent turn."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.authority import RequestOwner
from flowly.session.manager import Session, SessionManager

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_DIAGNOSTIC_ID = re.compile(r"^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$", re.I)
MAX_MESSAGES = 4000
MAX_CONNECTIONS = 100


class VoiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise VoiceError("INVALID_PARAMS", f"{field} must be a valid bounded identity.")
    return value


def integer(value: Any, field: str, *, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VoiceError("INVALID_PARAMS", f"{field} is outside its supported range.")
    return value


def bounded_text(value: Any, field: str, *, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or "\x00" in value:
        raise VoiceError("INVALID_PARAMS", f"{field} is invalid or too long.")
    return value.strip()


def session_key(conversation_id: Any) -> str:
    return f"desktop:voice:{identity(conversation_id, 'conversationId')}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata(session: Session) -> dict:
    if session.metadata.get("kind") != "voice":
        raise VoiceError("NOT_FOUND", "Voice conversation not found.")
    return session.metadata["voice"]


def _public(session: Session) -> dict:
    voice = _metadata(session)
    return {
        "conversationId": session.metadata["voiceConversationId"],
        "sessionKey": session.key, "kind": "voice", "voiceProtocolVersion": 1, "title": session.metadata["title"],
        "profile": voice["profile"], "botId": voice["botId"],
        "revision": voice["revision"], "focusTaskId": voice.get("focusTaskId"),
        "lastConnection": copy.deepcopy(voice["connections"][-1]),
        "spokenLanguage": voice.get("spokenLanguage", voice["connections"][-1]["language"]),
        "createdAt": session.created_at.isoformat(), "updatedAt": voice.get("updatedAt", session.created_at.isoformat()),
    }


class VoiceSessions:
    def __init__(self, sessions: SessionManager, *, principal: VoicePrincipal | None = None):
        self.sessions = sessions
        self._owner = {'kind': 'account', 'uid': principal.uid} if principal else {'kind': 'host'}

    def for_principal(self, principal: VoicePrincipal | None) -> VoiceSessions:
        """A request-local view; identity must come from verified access, not RPC data."""
        return VoiceSessions(self.sessions, principal=principal)

    def for_owner(self, owner: RequestOwner) -> VoiceSessions:
        view = VoiceSessions(self.sessions)
        view._owner = {'kind': 'account', 'uid': owner.uid} if owner.uid is not None else {'kind': 'host'}
        return view

    def _metadata(self, session: Session) -> dict:
        if session.metadata.get('voiceOwner', {'kind': 'host'}) != self._owner:
            raise VoiceError('NOT_FOUND', 'Voice conversation not found.')
        return _metadata(session)

    def open(self, params: dict, *, profile: str, bot_id: str) -> dict:
        key = session_key(params.get("conversationId"))
        connection_id = identity(params.get("connectionId"), "connectionId")
        candidate_run_id = params.get('voiceRunId')
        diagnostic_run_id = candidate_run_id.lower() if isinstance(candidate_run_id, str) and _DIAGNOSTIC_ID.fullmatch(candidate_run_id) else None
        language = params.get("language", "en")
        if not isinstance(language, str) or language not in {"en", "tr", "es"}:
            raise VoiceError("INVALID_PARAMS", "Unsupported conversation language.")

        def update(session: Session) -> dict:
            if session.metadata and session.metadata.get("kind") != "voice":
                raise VoiceError("CONFLICT", "This conversation identity is already in use.")
            if session.metadata and self._metadata(session).get("deletedAt"):
                raise VoiceError("NOT_FOUND", "Voice conversation was deleted.")
            if not session.metadata:
                session.metadata.update({
                    "kind": "voice", "voiceConversationId": params["conversationId"],
                    "voiceOwner": dict(self._owner),
                    "title": {"tr": "Sesli sohbet", "es": "Conversación de voz", "en": "Voice chat"}[language],
                    "voice": {"profile": profile, "botId": bot_id, "revision": 0, "connections": [], "spokenLanguage": language},
                })
            voice = self._metadata(session)
            if (voice["profile"], voice["botId"]) != (profile, bot_id):
                raise VoiceError("TARGET_CONFLICT", "Resume this conversation with its original agent.")
            existing = next((c for c in voice["connections"] if c["id"] == connection_id), None)
            if existing:
                if existing["language"] != language:
                    raise VoiceError("CONFLICT", "Connection identity already has different settings.")
                if existing is not voice["connections"][-1] or existing["endedAt"]:
                    raise VoiceError("STALE_CONNECTION", "Create a new connection to resume this conversation.")
                return _public(session)
            if len(voice["connections"]) >= MAX_CONNECTIONS:
                raise VoiceError("LIMIT", "Start a new voice conversation to continue.")
            voice["connections"].append({
                "id": connection_id, "generation": len(voice["connections"]) + 1,
                "language": language, "openedAt": _now(), "endedAt": None,
                **({'diagnosticRunId': diagnostic_run_id} if diagnostic_run_id else {}),
            })
            voice["revision"] += 1
            session.updated_at = datetime.now()
            voice["updatedAt"] = session.updated_at.isoformat()
            return _public(session)

        return self.sessions.mutate(key, update)

    def diagnostic_identity(self, params: dict) -> dict:
        """Resolve from owned storage, including retired connections; never latest.

        Optional diagnostics must not change command acceptance or idempotency.
        Callers isolate lookup failures from the actual RPC result.
        """
        session = self._read(params.get('conversationId'))
        voice = self._metadata(session)
        connection_id = params.get('connectionId')
        connection = next((c for c in voice['connections'] if c['id'] == connection_id), None)
        if not connection:
            return {}
        run_id = connection.get('diagnosticRunId')
        binding = {key: value.lower() for key, value in {'runId': run_id, 'connectionId': connection_id}.items()
                   if isinstance(value, str) and _DIAGNOSTIC_ID.fullmatch(value)}
        if binding.get('connectionId') and self._owner.get('kind') == 'account':
            material = json.dumps([self._owner['uid'], connection_id], ensure_ascii=False, separators=(',', ':'))
            binding['sessionRef'] = hashlib.sha256(material.encode()).hexdigest()
            if binding.get('runId'):
                run_material = json.dumps(['live-voice-run-v1', self._owner['uid'], binding['runId']], ensure_ascii=False, separators=(',', ':'))
                binding['runRef'] = hashlib.sha256(run_material.encode()).hexdigest()
        return binding

    def get(self, conversation_id: Any) -> dict:
        return _public(self._read(conversation_id))

    def list(self, params: dict) -> dict:
        offset = integer(params.get("offset", 0), "offset", maximum=100_000)
        limit = integer(params.get("limit", 50), "limit", minimum=1, maximum=100)
        conversations = []
        for row in self.sessions.list_sessions():
            if row.get("kind") != "voice":
                continue
            if row.get('voiceOwner', {'kind': 'host'}) != self._owner:
                continue
            conversation_id = row.get("voiceConversationId")
            if not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id):
                continue
            if not isinstance(row.get("voiceProfile"), str) or not isinstance(row.get("voiceBotId"), str):
                continue
            session = self.sessions.read(session_key(conversation_id))
            if session is None or self._metadata(session).get("deletedAt"):
                continue
            conversations.append({
                "conversationId": conversation_id, "kind": "voice", "voiceProtocolVersion": 1,
                "profile": row["voiceProfile"], "botId": row["voiceBotId"],
                "title": row.get("title") or "Voice chat", "updatedAt": row.get("updated_at"),
            })
        return {"conversations": conversations[offset:offset + limit],
                "nextOffset": offset + limit if offset + limit < len(conversations) else None}

    def _read(self, conversation_id: Any, *, include_deleted: bool = False) -> Session:
        session = self.sessions.read(session_key(conversation_id))
        if session is None:
            raise VoiceError("NOT_FOUND", "Voice conversation not found.")
        voice = self._metadata(session)
        if voice.get("deletedAt") and not include_deleted:
            raise VoiceError("NOT_FOUND", "Voice conversation not found.")
        return session

    def get_for_work(self, conversation_id: Any) -> dict:
        return _public(self._read(conversation_id, include_deleted=True))

    def delete(self, params: dict, *, preserve_work: bool = False) -> dict:
        conversation_id = identity(params.get("conversationId"), "conversationId")
        empty_only = params.get("ifEmpty", False)
        if not isinstance(empty_only, bool):
            raise VoiceError("INVALID_PARAMS", "ifEmpty must be a boolean.")
        key = session_key(conversation_id)

        class _NotEmpty(RuntimeError):
            pass

        def tombstone(current: Session) -> None:
            current_voice = self._metadata(current)
            if current_voice.get("deletedAt"):
                return
            connection = current_voice["connections"][-1]
            if not connection["endedAt"]:
                raise VoiceError("BUSY", "End the active voice connection before deleting this conversation.")
            has_content = bool(current.messages or current_voice.get("notices") or current_voice.get("focusTaskId"))
            if empty_only and has_content:
                raise _NotEmpty
            # The tombstone is written while holding the session lock. It
            # closes the race where a final transcript append could otherwise
            # land between an emptiness check and physical deletion.
            current.messages.clear()
            current_voice.pop("notices", None)
            current_voice.pop("focusTaskId", None)
            current_voice["deletedAt"] = _now()
            current_voice["revision"] += 1
            current.metadata["title"] = "Deleted voice chat"
            current.updated_at = datetime.now()
            current_voice["updatedAt"] = current.updated_at.isoformat()

        try:
            self.sessions.mutate(key, tombstone)
        except _NotEmpty:
            return {"deleted": False, "conversationId": conversation_id, "reason": "not_empty"}
        if preserve_work:
            # Task chats retain only the minimum conversation ownership shell;
            # the voice transcript and its display archive are both erased.
            self.sessions.delete_archive(key)
        else:
            self.sessions.delete(key)
        return {"deleted": True, "conversationId": conversation_id, "preservedWork": preserve_work}

    def require_connection(self, params: dict) -> dict:
        conversation = self.get(params.get("conversationId"))
        connection = conversation["lastConnection"]
        if connection["id"] != identity(params.get("connectionId"), "connectionId") or connection["endedAt"]:
            raise VoiceError("STALE_CONNECTION", "This voice connection no longer accepts commands.")
        return conversation

    def append(self, params: dict) -> dict:
        key = session_key(params.get("conversationId"))
        connection_id = identity(params.get("connectionId"), "connectionId")
        message_id = identity(params.get("messageId"), "messageId")
        ordinal = integer(params.get("ordinal"), "ordinal", maximum=2**32 - 1)
        revision = integer(params.get("revision", 1), "revision", minimum=1, maximum=100)
        role = params.get("role")
        if role not in {"user", "assistant"}:
            raise VoiceError("INVALID_PARAMS", "Voice transcripts accept only user and assistant speech.")
        continuation = params.get("continuesMessageId")
        if continuation is not None:
            if role != "assistant" or not isinstance(continuation, str) or continuation.count(":") != 1:
                raise VoiceError("INVALID_PARAMS", "Invalid speech continuation.")
            parent_connection, parent_message = continuation.split(":")
            identity(parent_connection, "continuation connection")
            identity(parent_message, "continuation message")
        delivery = params.get("delivery", "unknown")
        text = bounded_text(params.get("text"), "text", maximum=8000,
                            empty=role == "assistant" and delivery == "interrupted")
        if delivery not in {"unknown", "played", "interrupted"} or (role == "user" and delivery != "unknown"):
            raise VoiceError("INVALID_PARAMS", "Invalid speech delivery state.")
        # A correction targets the same provider message, even after reconnect.
        row_id = hashlib.sha256(f"{connection_id}:{message_id}".encode()).hexdigest()

        def update(session: Session) -> dict:
            voice = self._metadata(session)
            if voice.get("deletedAt"):
                raise VoiceError("NOT_FOUND", "Voice conversation not found.")
            connection = next((c for c in voice["connections"] if c["id"] == connection_id), None)
            if not connection:
                raise VoiceError("STALE_CONNECTION", "Unknown voice connection.")
            old = next((m for m in session.messages if m.get("voice", {}).get("messageId") == row_id), None)
            current = {"connectionId": connection_id, "messageId": row_id, "providerMessageId": message_id,
                       "generation": connection["generation"], "ordinal": ordinal, "revision": revision, "delivery": delivery}
            if continuation is not None:
                parent = next((m for m in session.messages
                               if m.get("voice", {}).get("connectionId") == parent_connection
                               and m["voice"].get("providerMessageId") == parent_message), None)
                if not parent or parent["role"] != "assistant" or parent["voice"]["generation"] >= connection["generation"]:
                    raise VoiceError("CONFLICT", "Continuation must reference an earlier assistant connection.")
                current["continuesMessageId"] = continuation
            if old:
                previous = old["voice"]
                if previous.get("continuesMessageId") != continuation:
                    raise VoiceError("CONFLICT", "Speech continuation identity cannot change.")
                if old["role"] != role or previous["ordinal"] != ordinal:
                    raise VoiceError("CONFLICT", "Message identity belongs to another transcript position.")
                if revision < previous["revision"]:
                    return {"message": copy.deepcopy(old), "replayed": True}
                if revision == previous["revision"]:
                    if old["content"] != text or previous["delivery"] != delivery:
                        raise VoiceError("CONFLICT", "Transcript revision already contains different content.")
                    return {"message": copy.deepcopy(old), "replayed": True}
                if previous["delivery"] == "interrupted" and delivery != "interrupted":
                    raise VoiceError("CONFLICT", "Interrupted speech cannot later be marked as fully played.")
                old.update({"content": text, "voice": current})
                row = old
            else:
                if len(session.messages) >= MAX_MESSAGES:
                    raise VoiceError("LIMIT", "Start a new voice conversation to continue.")
                if any(m.get("voice", {}).get("connectionId") == connection_id and m["voice"]["ordinal"] == ordinal
                       for m in session.messages):
                    raise VoiceError("CONFLICT", "Transcript position is already occupied.")
                session.add_message(role, text, kind="voice", voice=current, run_id=row_id)
                row = session.messages[-1]
            session.messages.sort(key=lambda m: (m["voice"]["generation"], m["voice"]["ordinal"]))
            if role == "user" and len([m for m in session.messages if m["role"] == "user"]) == 1:
                session.metadata["title"] = text[:80]
            voice["revision"] += 1
            session.updated_at = datetime.now()
            voice["updatedAt"] = session.updated_at.isoformat()
            return {"message": copy.deepcopy(row), "replayed": False}

        return self.sessions.mutate(key, update)

    def history(self, params: dict) -> dict:
        session = self._read(params.get("conversationId"))
        conversation = _public(session)
        offset = integer(params.get("offset", 0), "offset", maximum=MAX_MESSAGES)
        limit = integer(params.get("limit", 200), "limit", minimum=1, maximum=500)
        rows = session.messages
        # History is a revisioned snapshot, not an append-only event cursor:
        # provider corrections can revise earlier messages.
        return {"conversation": conversation, "messages": rows[offset:offset + limit],
                "notices": copy.deepcopy(self._metadata(session).get("notices", {})) if offset == 0 else {},
                "nextOffset": offset + limit if offset + limit < len(rows) else None}

    def notice(self, params: dict, *, task_id: str, event_id: int) -> dict:
        key = session_key(params.get("conversationId"))
        connection_id = identity(params.get("connectionId"), "connectionId")
        delivery = params.get("delivery")
        if delivery not in {"started", "played", "interrupted", "failed"}:
            raise VoiceError("INVALID_PARAMS", "Invalid task notice delivery.")

        def update(session: Session) -> dict:
            voice = self._metadata(session)
            notices = voice.setdefault("notices", {})
            existing = notices.get(str(event_id))
            if existing:
                if delivery == "started" or existing["delivery"] != "started":
                    return {"accepted": False, "notice": copy.deepcopy(existing)}
                if existing["connectionId"] != connection_id:
                    raise VoiceError("STALE_CONNECTION", "Another connection owns this notice.")
            else:
                current = voice["connections"][-1]
                if delivery != "started" or current["id"] != connection_id or current["endedAt"]:
                    raise VoiceError("STALE_CONNECTION", "Start notice delivery on the active voice connection.")
                if len(notices) >= 2000:
                    raise VoiceError("LIMIT", "Start a new voice conversation to continue.")
            notice = {"eventId": event_id, "taskId": task_id, "connectionId": connection_id,
                      "delivery": delivery, "updatedAt": _now()}
            notices[str(event_id)] = notice
            voice["revision"] += 1
            voice["updatedAt"] = _now()
            return {"accepted": True, "notice": copy.deepcopy(notice)}

        return self.sessions.mutate(key, update)

    def focus(self, params: dict, task_id: str | None) -> dict:
        key = session_key(params.get("conversationId"))
        connection_id = identity(params.get("connectionId"), "connectionId")
        revision = integer(params.get("expectedRevision"), "expectedRevision")

        def update(session: Session) -> dict:
            voice = self._metadata(session)
            connection = voice["connections"][-1]
            if connection["id"] != connection_id or connection["endedAt"]:
                raise VoiceError("STALE_CONNECTION", "This voice connection no longer accepts commands.")
            if voice.get("focusTaskId") == task_id:
                return _public(session)
            if voice["revision"] != revision:
                raise VoiceError("CONFLICT", "Conversation changed. Read its current focus before switching.")
            voice["focusTaskId"] = task_id
            voice["revision"] += 1
            session.updated_at = datetime.now()
            voice["updatedAt"] = session.updated_at.isoformat()
            return _public(session)

        return self.sessions.mutate(key, update)

    def set_language(self, params: dict) -> dict:
        """Persist the latest successful output-language event on this connection."""
        key = session_key(params.get("conversationId"))
        connection_id = identity(params.get("connectionId"), "connectionId")
        event_id = integer(params.get("providerEventId"), "providerEventId")
        language = params.get("language")
        if not isinstance(language, str) or language not in {"en", "tr", "es"}:
            raise VoiceError("INVALID_PARAMS", "Unsupported conversation language.")

        def update(session: Session) -> dict:
            voice = self._metadata(session)
            connection = voice["connections"][-1]
            # Cleanup may finish a pending write after End, but a retired
            # connection can never overwrite a newer connection's preference.
            if connection["id"] != connection_id:
                raise VoiceError("STALE_CONNECTION", "This voice connection no longer accepts language changes.")
            if event_id <= connection.get("languageEventId", -1):
                return _public(session)
            connection["languageEventId"] = event_id
            voice["spokenLanguage"] = language
            voice["revision"] += 1
            session.updated_at = datetime.now()
            voice["updatedAt"] = session.updated_at.isoformat()
            return _public(session)

        return self.sessions.mutate(key, update)

    def end(self, params: dict) -> dict:
        key = session_key(params.get("conversationId"))
        connection_id = identity(params.get("connectionId"), "connectionId")

        def update(session: Session) -> dict:
            voice = self._metadata(session)
            connection = next((c for c in voice["connections"] if c["id"] == connection_id), None)
            if not connection:
                raise VoiceError("NOT_FOUND", "Voice connection not found.")
            if not connection["endedAt"]:
                connection["endedAt"] = _now()
                voice["revision"] += 1
                session.updated_at = datetime.now()
                voice["updatedAt"] = session.updated_at.isoformat()
            return _public(session)

        return self.sessions.mutate(key, update)
