"""Durable, summary-independent task continuity for agent sessions.

Conversation summaries are lossy by design.  This store keeps the small set of
facts that must not depend on a summariser choosing to mention them: the active
objective, the latest user instruction, cancellation state, and the archive
event that contains the exact instruction.  The current snapshot is written
atomically and every revision is also appended to an audit log.

The store intentionally does not infer intent.  ``AgentLoop`` owns the
deterministic policy that decides whether a message starts a new objective or
is an explicit reversal; this module only persists that decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.plans.store import safe_filename

TASK_STATE_SCHEMA_VERSION = 1
MAX_STORED_TEXT_CHARS = 64_000
MAX_PROMPT_TEXT_CHARS = 6_000
_TRUNCATION_MARKER = "… [truncated]"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


@dataclass(frozen=True)
class TaskState:
    """The authoritative task snapshot for one conversation."""

    session_key: str
    revision: int = 0
    objective: str = ""
    latest_user_request: str = ""
    latest_user_request_sha256: str = ""
    latest_user_event_id: str = ""
    status: str = "idle"
    updated_at: str = ""
    schema_version: int = TASK_STATE_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskState":
        status = str(raw.get("status") or "idle")
        if status not in {"idle", "active", "cancelled", "completed", "cleared"}:
            status = "idle"
        return cls(
            session_key=str(raw.get("session_key") or ""),
            revision=max(0, int(raw.get("revision") or 0)),
            objective=str(raw.get("objective") or ""),
            latest_user_request=str(raw.get("latest_user_request") or ""),
            latest_user_request_sha256=str(
                raw.get("latest_user_request_sha256") or ""
            ),
            latest_user_event_id=str(raw.get("latest_user_event_id") or ""),
            status=status,
            updated_at=str(raw.get("updated_at") or ""),
            schema_version=TASK_STATE_SCHEMA_VERSION,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStateStore:
    """Thread-safe, atomic, disk-backed task snapshots.

    ``persist=False`` is useful for isolated tests.  Production defaults to a
    dedicated directory below the Flowly home so the state survives process
    restarts and is shared by every transport using the same gateway.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        persist: bool = True,
        hydrate: bool = True,
    ) -> None:
        self._root_override = root
        self._root: Path | None = None
        self._persist = persist
        self._states: dict[str, TaskState] = {}
        self._lock = threading.RLock()
        if hydrate:
            self.hydrate()

    def _resolve_root(self) -> Path | None:
        if not self._persist:
            return None
        if self._root is not None:
            return self._root
        if self._root_override is not None:
            root = self._root_override
        else:
            try:
                from flowly.profile import get_flowly_home

                root = get_flowly_home() / "task-state"
            except Exception as exc:  # noqa: BLE001 - task turns must continue
                logger.warning("[task-state] cannot resolve storage: {}", exc)
                self._persist = False
                return None
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[task-state] cannot create storage: {}", exc)
            self._persist = False
            return None
        self._root = root
        return root

    @staticmethod
    def _file_stem(session_key: str) -> str:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:16]
        return f"{safe_filename(session_key)}-{digest}"

    def _snapshot_path(self, session_key: str) -> Path | None:
        root = self._resolve_root()
        if root is None:
            return None
        return root / f"{self._file_stem(session_key)}.json"

    def hydrate(self) -> int:
        root = self._resolve_root()
        if root is None or not root.exists():
            return 0
        loaded = 0
        for path in root.glob("*.json"):
            try:
                state = TaskState.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if not state.session_key:
                    raise ValueError("missing session_key")
            except Exception as exc:  # noqa: BLE001 - one corrupt file is isolated
                logger.warning("[task-state] skip corrupt snapshot {}: {}", path, exc)
                continue
            with self._lock:
                existing = self._states.get(state.session_key)
                if existing is None or state.revision >= existing.revision:
                    self._states[state.session_key] = state
            loaded += 1
        if loaded:
            logger.info("[task-state] hydrated {} task snapshot(s)", loaded)
        return loaded

    def get(self, session_key: str) -> TaskState | None:
        with self._lock:
            return self._states.get(session_key)

    def observe_user_message(
        self,
        session_key: str,
        content: str,
        *,
        new_objective: str | None = None,
        cancel: bool = False,
        event_id: str = "",
    ) -> TaskState:
        """Record one user message and an already-classified state transition."""
        content = str(content or "")
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored_content = _cap(content, MAX_STORED_TEXT_CHARS)
        with self._lock:
            previous = self._states.get(session_key) or TaskState(
                session_key=session_key
            )
            objective = previous.objective
            status = previous.status
            if cancel:
                status = "cancelled"
            elif new_objective is not None and new_objective.strip():
                objective = _cap(new_objective.strip(), MAX_STORED_TEXT_CHARS)
                status = "active"
            state = replace(
                previous,
                revision=previous.revision + 1,
                objective=objective,
                latest_user_request=stored_content,
                latest_user_request_sha256=content_digest,
                # A newly observed message has no canonical archive identity
                # until the turn save succeeds. Never leave the previous
                # request's id attached to the new request in that window.
                latest_user_event_id=str(event_id or ""),
                status=status,
                updated_at=_utc_now(),
            )
            self._states[session_key] = state
            # Keep mutation and write-through under one lock so two turns on
            # different worker threads cannot persist revision N+1 and then
            # overwrite it with the slower revision N.
            self._write(state, transition="cancel" if cancel else "observe")
        return state

    def bind_event(self, session_key: str, event_id: str) -> TaskState | None:
        """Attach the stable archive event minted by the canonical turn save."""
        event_id = str(event_id or "").strip()
        if not event_id:
            return self.get(session_key)
        with self._lock:
            previous = self._states.get(session_key)
            if previous is None or previous.latest_user_event_id == event_id:
                return previous
            state = replace(
                previous,
                revision=previous.revision + 1,
                latest_user_event_id=event_id,
                updated_at=_utc_now(),
            )
            self._states[session_key] = state
            self._write(state, transition="bind_event")
        return state

    def reset(self, session_key: str) -> TaskState:
        """Clear the live contract while retaining a revisioned audit record."""
        with self._lock:
            previous = self._states.get(session_key) or TaskState(
                session_key=session_key
            )
            state = TaskState(
                session_key=session_key,
                revision=previous.revision + 1,
                status="cleared",
                updated_at=_utc_now(),
            )
            self._states[session_key] = state
            self._write(state, transition="reset")
        return state

    def prompt_sidecar(self, session_key: str) -> str:
        """Render a bounded, request-only continuity block for the model."""
        state = self.get(session_key)
        if state is None or state.status != "active" or not state.objective:
            return ""
        objective = _cap(state.objective, MAX_PROMPT_TEXT_CHARS)
        latest = _cap(state.latest_user_request, MAX_PROMPT_TEXT_CHARS)
        # JSON encoding prevents user text from forging surrounding prompt
        # markup while remaining directly readable by every provider.
        payload = {
            "revision": state.revision,
            "status": state.status,
            "objective": objective,
            "latest_user_request": latest,
            "latest_user_event_id": state.latest_user_event_id or None,
        }
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        # Keep user-controlled ``<``/``>``/``&`` inside JSON escapes so text
        # cannot forge a closing tag around this backend-authored envelope.
        encoded_payload = (
            encoded_payload.replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        return (
            "<durable_task_state source=\"backend\" authority=\"task_contract\">\n"
            + encoded_payload
            + "\n</durable_task_state>\n"
            "Keep the objective active across summaries and compaction. The "
            "latest user request may refine or constrain it; explicit current "
            "user instructions still take precedence."
        )

    def _write(self, state: TaskState, *, transition: str) -> None:
        path = self._snapshot_path(state.session_key)
        if path is None:
            return
        payload = state.as_dict()
        tmp = path.with_name(
            f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            with tmp.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            # Persist the directory entry too; without this fsync, sudden
            # power loss can theoretically lose an otherwise-fsynced rename.
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not permit directory fsync. The atomic
                # snapshot still remains valid there.
                pass
            audit_path = path.with_suffix(".revisions.jsonl")
            audit_row = {
                "transition": transition,
                **payload,
            }
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(audit_row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("[task-state] persistence failed for {}: {}", path, exc)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
