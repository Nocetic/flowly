"""Append-only conversation archive primitives.

The working session file is allowed to shrink after compaction.  The sibling
``.full.jsonl`` file is not: it is the durable event lineage used by display,
recall, coverage checks, and audit.  Message rows are immutable; state changes
are appended as transition records so a crash can never leave a half-rewritten
archive.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from flowly.compaction.types import is_context_boundary

ArchiveState = Literal["active", "compacted", "withdrawn", "internal_hidden"]

EVENT_ID_KEY = "_event_id"
EVENT_SEQ_KEY = "_event_seq"
ARCHIVE_STATE_KEY = "_archive_state"
ARCHIVE_SUMMARY_KEY = "_archive_summary"
ARCHIVE_TRANSITION_TYPE = "archive_state"
ARCHIVE_COMMIT_TYPE = "archive_commit"
ARCHIVE_TRANSACTION_KEY = "_archive_transaction_id"
SUMMARY_COVERS_KEY = "summary_covers"

VALID_ARCHIVE_STATES: frozenset[str] = frozenset(
    {"active", "compacted", "withdrawn", "internal_hidden"}
)


def message_fingerprint(message: dict[str, Any]) -> str:
    """Stable legacy identity material shared by canonical and full logs."""
    material = {
        "role": message.get("role"),
        "content": message.get("content"),
        "timestamp": message.get("timestamp"),
        "tool_call_id": message.get("tool_call_id"),
        "name": message.get("name"),
        "tool_calls": message.get("tool_calls"),
    }
    raw = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def legacy_event_id(message: dict[str, Any], occurrence: int = 0) -> str:
    """Deterministic id for rows written before archive identities existed."""
    digest = message_fingerprint(message)[:24]
    return f"evt_legacy_{digest}_{max(0, occurrence):04x}"


def new_event_id(sequence: int) -> str:
    """Opaque stable id with a human-sortable sequence prefix."""
    return f"evt_{max(1, sequence):012x}_{uuid.uuid4().hex[:12]}"


def initial_state(message: dict[str, Any]) -> ArchiveState:
    if message.get("_display_hidden"):
        return "internal_hidden"
    return "active"


def ensure_event_identity(
    message: dict[str, Any],
    *,
    sequence: int,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Attach archive identity/state in place and return ``message``."""
    seq = max(1, int(message.get(EVENT_SEQ_KEY) or sequence))
    message[EVENT_SEQ_KEY] = seq
    if not message.get(EVENT_ID_KEY):
        message[EVENT_ID_KEY] = event_id or new_event_id(seq)
    state = str(message.get(ARCHIVE_STATE_KEY) or initial_state(message))
    message[ARCHIVE_STATE_KEY] = (
        state if state in VALID_ARCHIVE_STATES else initial_state(message)
    )
    return message


@dataclass(frozen=True)
class ArchiveEvent:
    event_id: str
    sequence: int
    state: ArchiveState
    message: dict[str, Any]


@dataclass(frozen=True)
class ArchiveSnapshot:
    events: tuple[ArchiveEvent, ...]

    @property
    def by_id(self) -> dict[str, ArchiveEvent]:
        return {event.event_id: event for event in self.events}

    @property
    def max_sequence(self) -> int:
        return max((event.sequence for event in self.events), default=0)

    def state_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in VALID_ARCHIVE_STATES}
        for event in self.events:
            counts[event.state] += 1
        return counts


def snapshot_from_rows(rows: list[dict[str, Any]]) -> ArchiveSnapshot:
    """Materialise current event state from immutable rows + transitions."""
    committed_transactions = {
        str(row.get("transaction_id"))
        for row in rows
        if isinstance(row, dict)
        and row.get("_type") == ARCHIVE_COMMIT_TYPE
        and row.get("transaction_id")
    }
    events: list[ArchiveEvent] = []
    index_by_id: dict[str, int] = {}
    occurrences: dict[str, int] = {}
    pending_states: dict[str, ArchiveState] = {}
    next_sequence = 1

    for row in rows:
        if not isinstance(row, dict):
            continue
        transaction_id = str(row.get(ARCHIVE_TRANSACTION_KEY) or "")
        if transaction_id and transaction_id not in committed_transactions:
            continue
        if row.get("_type") == ARCHIVE_TRANSITION_TYPE:
            transition_tx = str(row.get("transaction_id") or "")
            if transition_tx and transition_tx not in committed_transactions:
                continue
            state = str(row.get("state") or "")
            if state not in VALID_ARCHIVE_STATES:
                continue
            for raw_id in row.get("event_ids") or []:
                event_id = str(raw_id or "")
                if not event_id:
                    continue
                pending_states[event_id] = state  # last transition wins
                existing_index = index_by_id.get(event_id)
                if existing_index is not None:
                    existing = events[existing_index]
                    events[existing_index] = ArchiveEvent(
                        event_id=existing.event_id,
                        sequence=existing.sequence,
                        state=state,  # type: ignore[arg-type]
                        message=existing.message,
                    )
            continue
        if row.get("_type") == "metadata" or is_context_boundary(row):
            continue
        if not row.get("role"):
            continue

        message = dict(row)
        fingerprint = message_fingerprint(message)
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        event_id = str(message.get(EVENT_ID_KEY) or "")
        if not event_id:
            event_id = legacy_event_id(message, occurrence)
            message[EVENT_ID_KEY] = event_id
        try:
            sequence = int(message.get(EVENT_SEQ_KEY) or next_sequence)
        except (TypeError, ValueError):
            sequence = next_sequence
        sequence = max(1, sequence)
        next_sequence = max(next_sequence, sequence + 1)
        message[EVENT_SEQ_KEY] = sequence

        raw_state = pending_states.get(
            event_id,
            str(message.get(ARCHIVE_STATE_KEY) or initial_state(message)),
        )
        state: ArchiveState = (
            raw_state if raw_state in VALID_ARCHIVE_STATES else initial_state(message)
        )  # type: ignore[assignment]
        message[ARCHIVE_STATE_KEY] = state
        if event_id in index_by_id:
            # A retried append of the same immutable event is idempotent.
            continue
        index_by_id[event_id] = len(events)
        events.append(ArchiveEvent(event_id, sequence, state, message))

    events.sort(key=lambda event: (event.sequence, event.event_id))
    return ArchiveSnapshot(tuple(events))


def transition_record(
    event_ids: list[str],
    state: ArchiveState,
    *,
    timestamp: str,
    reason: str = "",
    compaction_id: str = "",
    transaction_id: str = "",
) -> dict[str, Any]:
    """One append-only state transition for a set of immutable events."""
    if state not in VALID_ARCHIVE_STATES:
        raise ValueError(f"invalid archive state: {state}")
    record: dict[str, Any] = {
        "_type": ARCHIVE_TRANSITION_TYPE,
        "event_ids": list(dict.fromkeys(event_ids)),
        "state": state,
        "timestamp": timestamp,
    }
    if reason:
        record["reason"] = reason
    if compaction_id:
        record["compaction_id"] = compaction_id
    if transaction_id:
        record["transaction_id"] = transaction_id
    return record


def commit_record(transaction_id: str, *, timestamp: str) -> dict[str, Any]:
    if not transaction_id:
        raise ValueError("archive transaction id is required")
    return {
        "_type": ARCHIVE_COMMIT_TYPE,
        "transaction_id": transaction_id,
        "timestamp": timestamp,
    }


def transaction_is_committed(
    rows: list[dict[str, Any]], transaction_id: str,
) -> bool:
    return any(
        row.get("_type") == ARCHIVE_COMMIT_TYPE
        and row.get("transaction_id") == transaction_id
        for row in rows
        if isinstance(row, dict)
    )


def coverage_descriptor(events: list[ArchiveEvent]) -> dict[str, Any]:
    """Exact, tamper-evident description of events represented by a summary."""
    ordered = sorted(events, key=lambda event: (event.sequence, event.event_id))
    event_ids = [event.event_id for event in ordered]
    digest = hashlib.sha256("\n".join(event_ids).encode("utf-8")).hexdigest()
    return {
        "start_event_id": event_ids[0] if event_ids else "",
        "end_event_id": event_ids[-1] if event_ids else "",
        "start_sequence": ordered[0].sequence if ordered else 0,
        "end_sequence": ordered[-1].sequence if ordered else 0,
        "event_count": len(ordered),
        "event_ids_sha256": digest,
    }


def messages_equivalent(
    archived: dict[str, Any],
    projected: dict[str, Any],
) -> bool:
    """Match a compacted request copy back to its immutable archive event."""
    if archived.get("role") != projected.get("role"):
        return False
    role = archived.get("role")
    if role == "tool":
        return (
            archived.get("tool_call_id") == projected.get("tool_call_id")
            and archived.get("name") == projected.get("name")
        )
    if role == "assistant" and (
        archived.get("tool_calls") or projected.get("tool_calls")
    ):
        archived_ids = [
            str(call.get("call_id") or call.get("id") or "")
            for call in archived.get("tool_calls") or []
            if isinstance(call, dict)
        ]
        projected_ids = [
            str(call.get("call_id") or call.get("id") or "")
            for call in projected.get("tool_calls") or []
            if isinstance(call, dict)
        ]
        return archived_ids == projected_ids
    return archived.get("content") == projected.get("content")


def match_kept_events(
    source_messages: list[dict[str, Any]],
    kept_messages: list[dict[str, Any]],
) -> list[dict[str, Any] | None]:
    """Return source event metadata corresponding to each kept request row."""
    matches: list[dict[str, Any] | None] = [None] * len(kept_messages)
    source_index = len(source_messages) - 1
    for kept_index in range(len(kept_messages) - 1, -1, -1):
        kept = kept_messages[kept_index]
        while source_index >= 0:
            source = source_messages[source_index]
            source_index -= 1
            if messages_equivalent(source, kept):
                matches[kept_index] = {
                    EVENT_ID_KEY: source.get(EVENT_ID_KEY),
                    EVENT_SEQ_KEY: source.get(EVENT_SEQ_KEY),
                    ARCHIVE_STATE_KEY: source.get(ARCHIVE_STATE_KEY, "active"),
                    "_display_hidden": source.get("_display_hidden", False),
                }
                break
    return matches


@dataclass(frozen=True)
class ContextCoverageManifest:
    total_events: int
    active_events: int
    compacted_events: int
    withdrawn_events: int
    internal_hidden_events: int
    represented_events: int
    coverage_gap: int
    summary_covers: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "active_events": self.active_events,
            "compacted_events": self.compacted_events,
            "withdrawn_events": self.withdrawn_events,
            "internal_hidden_events": self.internal_hidden_events,
            "represented_events": self.represented_events,
            "coverage_gap": self.coverage_gap,
            "summary_covers": self.summary_covers,
        }

    def prompt_sidecar(self) -> str:
        status = "complete" if self.coverage_gap == 0 else "INCOMPLETE"
        return (
            "<conversation_coverage "
            f"status=\"{status}\" total=\"{self.total_events}\" "
            f"active=\"{self.active_events + self.internal_hidden_events}\" "
            f"summarized=\"{self.compacted_events}\" "
            f"withdrawn=\"{self.withdrawn_events}\" "
            f"coverage_gap=\"{self.coverage_gap}\" />"
        )


def build_coverage_manifest(
    snapshot: ArchiveSnapshot,
    working_messages: list[dict[str, Any]],
    summary_covers: dict[str, Any] | None,
) -> ContextCoverageManifest:
    """Prove every durable event is active, summarized, or withdrawn."""
    working_ids = {
        str(message.get(EVENT_ID_KEY))
        for message in working_messages
        if message.get(EVENT_ID_KEY)
    }
    summary_present = any(
        message.get(ARCHIVE_SUMMARY_KEY) and message.get(EVENT_ID_KEY) in working_ids
        for message in working_messages
    )
    start = int((summary_covers or {}).get("start_sequence") or 0)
    end = int((summary_covers or {}).get("end_sequence") or 0)

    represented = 0
    gap = 0
    counts = snapshot.state_counts()
    for event in snapshot.events:
        if event.state in ("active", "internal_hidden"):
            covered = event.event_id in working_ids
        elif event.state == "compacted":
            covered = summary_present and start <= event.sequence <= end
        else:  # withdrawn is intentionally absent from model context
            covered = True
        if covered:
            represented += 1
        else:
            gap += 1

    return ContextCoverageManifest(
        total_events=len(snapshot.events),
        active_events=counts["active"],
        compacted_events=counts["compacted"],
        withdrawn_events=counts["withdrawn"],
        internal_hidden_events=counts["internal_hidden"],
        represented_events=represented,
        coverage_gap=gap,
        summary_covers=summary_covers,
    )
