"""Validated data contracts for persistent goals.

The goal record is deliberately independent from plans, memory and transient
agent state.  It exists only after an explicit ``/goal`` command and carries a
generation id so queued continuations can be rejected after pause, clear or
replacement.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

STATE_SCHEMA_VERSION = 1
DEFAULT_MAX_TURNS = 20
DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3

MAX_GOAL_CHARS = 2_000
MAX_CONTRACT_FIELD_CHARS = 2_500
MAX_SUBGOAL_CHARS = 1_000
MAX_SUBGOALS = 50
MAX_GATE_COMMAND_CHARS = 4_000
MAX_GATES = 20
MAX_REASON_CHARS = 2_000
MAX_GATE_OUTPUT_CHARS = 3_000


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    CLEARED = "cleared"


class GoalVerdict(StrEnum):
    DONE = "done"
    CONTINUE = "continue"
    WAIT = "wait"
    INACTIVE = "inactive"
    SKIPPED = "skipped"
    GATE_FAILED = "gate_failed"
    WAITING = "waiting"


class WaitKind(StrEnum):
    PID = "pid"
    SESSION = "session"
    TIME = "time"


def _clean_text(value: Any, *, limit: int, field_name: str) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError(f"{field_name} exceeds {limit} characters")
    return text


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class GoalContract:
    """Optional, user-controlled definition of completion."""

    outcome: str = ""
    verification: str = ""
    constraints: str = ""
    boundaries: str = ""
    stop_when: str = ""

    def __post_init__(self) -> None:
        for name in ("outcome", "verification", "constraints", "boundaries", "stop_when"):
            setattr(
                self,
                name,
                _clean_text(
                    getattr(self, name),
                    limit=MAX_CONTRACT_FIELD_CHARS,
                    field_name=f"contract.{name}",
                ),
            )

    @property
    def is_empty(self) -> bool:
        return not any(self.to_dict().values())

    def to_dict(self) -> dict[str, str]:
        return {
            "outcome": self.outcome,
            "verification": self.verification,
            "constraints": self.constraints,
            "boundaries": self.boundaries,
            "stop_when": self.stop_when,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "GoalContract":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            outcome=data.get("outcome", ""),
            verification=data.get("verification", ""),
            constraints=data.get("constraints", ""),
            boundaries=data.get("boundaries", ""),
            stop_when=data.get("stop_when", data.get("stopWhen", "")),
        )

    def render(self) -> str:
        labels = (
            ("Outcome", self.outcome),
            ("Verification", self.verification),
            ("Constraints", self.constraints),
            ("Boundaries", self.boundaries),
            ("Stop when", self.stop_when),
        )
        return "\n".join(f"- {label}: {value}" for label, value in labels if value)


_CONTRACT_ALIASES: dict[str, str] = {
    "outcome": "outcome",
    "goal": "outcome",
    "done": "outcome",
    "done when": "outcome",
    "verification": "verification",
    "verify": "verification",
    "verified by": "verification",
    "evidence": "verification",
    "proof": "verification",
    "constraints": "constraints",
    "constraint": "constraints",
    "preserve": "constraints",
    "must not": "constraints",
    "do not change": "constraints",
    "boundaries": "boundaries",
    "boundary": "boundaries",
    "scope": "boundaries",
    "allowed": "boundaries",
    "files": "boundaries",
    "stop when": "stop_when",
    "stop_when": "stop_when",
    "blocked": "stop_when",
    "stop if blocked": "stop_when",
    "give up when": "stop_when",
}


def parse_contract(text: str) -> GoalContract:
    """Parse a small heading-based completion contract.

    Labels are case-insensitive and accept either ``Label: value`` or a label
    on its own line followed by one or more body lines. Unknown text is ignored
    so free-form goals remain free-form instead of being misclassified.
    """

    values: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = line.lstrip("#*- ").strip()
        label, separator, remainder = line.partition(":")
        canonical = _CONTRACT_ALIASES.get(label.strip().casefold())
        if separator and canonical:
            current = canonical
            values.setdefault(current, [])
            if remainder.strip():
                values[current].append(remainder.strip())
            continue
        canonical = _CONTRACT_ALIASES.get(line.casefold())
        if canonical:
            current = canonical
            values.setdefault(current, [])
            continue
        if current:
            values[current].append(line)

    return GoalContract(**{key: "\n".join(parts).strip() for key, parts in values.items() if parts})


@dataclass(slots=True)
class GoalGate:
    command: str
    timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_GATE_MAX_RETRIES
    attempts: int = 0
    last_exit_code: int | None = None
    last_output_tail: str = ""
    last_failed_fingerprint: str = ""

    def __post_init__(self) -> None:
        self.command = _clean_text(
            self.command,
            limit=MAX_GATE_COMMAND_CHARS,
            field_name="gate.command",
        )
        if not self.command:
            raise ValueError("gate command is empty")
        self.timeout_seconds = _bounded_int(
            self.timeout_seconds, default=DEFAULT_GATE_TIMEOUT_SECONDS, minimum=1, maximum=3_600
        )
        self.max_retries = _bounded_int(
            self.max_retries, default=DEFAULT_GATE_MAX_RETRIES, minimum=0, maximum=100
        )
        self.attempts = _bounded_int(self.attempts, default=0, minimum=0, maximum=1_000_000)
        if self.last_exit_code is not None:
            self.last_exit_code = int(self.last_exit_code)
        self.last_output_tail = str(self.last_output_tail or "")[-MAX_GATE_OUTPUT_CHARS:]
        self.last_failed_fingerprint = str(self.last_failed_fingerprint or "")[:128]

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "attempts": self.attempts,
            "last_exit_code": self.last_exit_code,
            "last_output_tail": self.last_output_tail,
            "last_failed_fingerprint": self.last_failed_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "GoalGate":
        if not isinstance(value, Mapping):
            raise ValueError("gate must be an object")
        return cls(
            command=value.get("command", ""),
            timeout_seconds=value.get(
                "timeout_seconds", value.get("timeoutSeconds", DEFAULT_GATE_TIMEOUT_SECONDS)
            ),
            max_retries=value.get("max_retries", value.get("maxRetries", DEFAULT_GATE_MAX_RETRIES)),
            attempts=value.get("attempts", 0),
            last_exit_code=value.get("last_exit_code", value.get("lastExitCode")),
            last_output_tail=value.get("last_output_tail", value.get("lastOutputTail", "")),
            last_failed_fingerprint=value.get(
                "last_failed_fingerprint", value.get("lastFailedFingerprint", "")
            ),
        )


@dataclass(slots=True)
class GoalState:
    session_key: str
    goal: str
    status: GoalStatus = GoalStatus.ACTIVE
    goal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = STATE_SCHEMA_VERSION
    revision: int = 0
    conversation_epoch: int = 0
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_turn_at: float = 0.0
    last_verdict: str | None = None
    last_reason: str | None = None
    paused_reason: str | None = None
    consecutive_parse_failures: int = 0
    consecutive_transport_failures: int = 0
    consecutive_compaction_failures: int = 0
    subgoals: list[str] = field(default_factory=list)
    waiting_on_pid: int | None = None
    waiting_on_session: str | None = None
    waiting_until: float = 0.0
    waiting_reason: str | None = None
    waiting_since: float = 0.0
    contract: GoalContract = field(default_factory=GoalContract)
    gates: list[GoalGate] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.session_key = _clean_text(self.session_key, limit=1_000, field_name="session_key")
        if not self.session_key:
            raise ValueError("session_key is required")
        self.goal = _clean_text(self.goal, limit=MAX_GOAL_CHARS, field_name="goal")
        self.status = GoalStatus(self.status)
        if self.status is not GoalStatus.CLEARED and not self.goal:
            raise ValueError("goal is required")
        self.goal_id = str(self.goal_id or "").strip() or uuid.uuid4().hex
        self.schema_version = _bounded_int(self.schema_version, default=1, minimum=1, maximum=1)
        self.revision = _bounded_int(self.revision, default=0, minimum=0, maximum=2**63 - 1)
        self.conversation_epoch = _bounded_int(
            self.conversation_epoch, default=0, minimum=0, maximum=2**63 - 1
        )
        self.turns_used = _bounded_int(self.turns_used, default=0, minimum=0, maximum=1_000_000)
        self.max_turns = _bounded_int(
            self.max_turns, default=DEFAULT_MAX_TURNS, minimum=1, maximum=10_000
        )
        self.created_at = _bounded_float(self.created_at, default=time.time())
        self.updated_at = _bounded_float(self.updated_at, default=self.created_at)
        self.last_turn_at = _bounded_float(self.last_turn_at)
        self.last_verdict = _clean_optional(self.last_verdict, MAX_REASON_CHARS)
        self.last_reason = _clean_optional(self.last_reason, MAX_REASON_CHARS)
        self.paused_reason = _clean_optional(self.paused_reason, MAX_REASON_CHARS)
        self.consecutive_parse_failures = _bounded_int(
            self.consecutive_parse_failures, default=0, minimum=0, maximum=1_000_000
        )
        self.consecutive_transport_failures = _bounded_int(
            self.consecutive_transport_failures, default=0, minimum=0, maximum=1_000_000
        )
        self.consecutive_compaction_failures = _bounded_int(
            self.consecutive_compaction_failures,
            default=0,
            minimum=0,
            maximum=1_000_000,
        )
        self.subgoals = _clean_unique_strings(
            self.subgoals, limit=MAX_SUBGOAL_CHARS, max_items=MAX_SUBGOALS, field_name="subgoal"
        )
        if self.waiting_on_pid is not None:
            self.waiting_on_pid = int(self.waiting_on_pid)
            if self.waiting_on_pid <= 0:
                self.waiting_on_pid = None
        self.waiting_on_session = _clean_optional(self.waiting_on_session, 1_000)
        self.waiting_until = _bounded_float(self.waiting_until)
        self.waiting_reason = _clean_optional(self.waiting_reason, MAX_REASON_CHARS)
        self.waiting_since = _bounded_float(self.waiting_since)
        if not isinstance(self.contract, GoalContract):
            self.contract = GoalContract.from_dict(self.contract)
        self.gates = [
            gate if isinstance(gate, GoalGate) else GoalGate.from_dict(gate) for gate in self.gates
        ]
        if len(self.gates) > MAX_GATES:
            raise ValueError(f"goal has more than {MAX_GATES} gates")
        self._validate_wait_target()

    @property
    def is_active(self) -> bool:
        return self.status is GoalStatus.ACTIVE

    @property
    def has_wait(self) -> bool:
        return any((self.waiting_on_pid, self.waiting_on_session, self.waiting_until))

    def _validate_wait_target(self) -> None:
        targets = sum(
            bool(value)
            for value in (self.waiting_on_pid, self.waiting_on_session, self.waiting_until)
        )
        if targets > 1:
            raise ValueError("goal state contains multiple wait targets")

    def clear_wait(self) -> None:
        self.waiting_on_pid = None
        self.waiting_on_session = None
        self.waiting_until = 0.0
        self.waiting_reason = None
        self.waiting_since = 0.0

    def set_wait(
        self,
        kind: WaitKind,
        target: int | str | float,
        *,
        reason: str = "",
        now: float | None = None,
    ) -> None:
        self.clear_wait()
        now = time.time() if now is None else float(now)
        if kind is WaitKind.PID:
            pid = int(target)
            if pid <= 0:
                raise ValueError("pid must be a positive integer")
            self.waiting_on_pid = pid
        elif kind is WaitKind.SESSION:
            session_id = _clean_text(target, limit=1_000, field_name="wait session")
            if not session_id:
                raise ValueError("session id is required")
            self.waiting_on_session = session_id
        elif kind is WaitKind.TIME:
            deadline = float(target)
            if deadline <= now:
                raise ValueError("wait deadline must be in the future")
            self.waiting_until = deadline
        else:  # pragma: no cover - StrEnum constrains callers
            raise ValueError(f"unsupported wait kind: {kind}")
        self.waiting_reason = _clean_optional(reason, MAX_REASON_CHARS)
        self.waiting_since = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_key": self.session_key,
            "goal_id": self.goal_id,
            "revision": self.revision,
            "conversation_epoch": self.conversation_epoch,
            "goal": self.goal,
            "status": self.status.value,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_turn_at": self.last_turn_at,
            "last_verdict": self.last_verdict,
            "last_reason": self.last_reason,
            "paused_reason": self.paused_reason,
            "consecutive_parse_failures": self.consecutive_parse_failures,
            "consecutive_transport_failures": self.consecutive_transport_failures,
            "consecutive_compaction_failures": self.consecutive_compaction_failures,
            "subgoals": list(self.subgoals),
            "waiting_on_pid": self.waiting_on_pid,
            "waiting_on_session": self.waiting_on_session,
            "waiting_until": self.waiting_until,
            "waiting_reason": self.waiting_reason,
            "waiting_since": self.waiting_since,
            "contract": self.contract.to_dict(),
            "gates": [gate.to_dict() for gate in self.gates],
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Stable camelCase snapshot for clients and relay transports."""
        return {
            "goalId": self.goal_id,
            "revision": self.revision,
            "conversationEpoch": self.conversation_epoch,
            "goal": self.goal,
            "status": self.status.value,
            "turnsUsed": self.turns_used,
            "maxTurns": self.max_turns,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastTurnAt": self.last_turn_at or None,
            "lastVerdict": self.last_verdict,
            "lastReason": self.last_reason,
            "pausedReason": self.paused_reason,
            "consecutiveCompactionFailures": self.consecutive_compaction_failures,
            "subgoals": list(self.subgoals),
            "wait": self.wait_public_dict(),
            "contract": self.contract.to_dict() if not self.contract.is_empty else None,
            "gates": [
                {
                    "command": gate.command,
                    "timeoutSeconds": gate.timeout_seconds,
                    "maxRetries": gate.max_retries,
                    "attempts": gate.attempts,
                    "lastExitCode": gate.last_exit_code,
                }
                for gate in self.gates
            ],
        }

    def wait_public_dict(self) -> dict[str, Any] | None:
        if self.waiting_on_pid:
            return {
                "kind": WaitKind.PID.value,
                "pid": self.waiting_on_pid,
                "reason": self.waiting_reason,
            }
        if self.waiting_on_session:
            return {
                "kind": WaitKind.SESSION.value,
                "sessionId": self.waiting_on_session,
                "reason": self.waiting_reason,
            }
        if self.waiting_until:
            return {
                "kind": WaitKind.TIME.value,
                "until": self.waiting_until,
                "reason": self.waiting_reason,
            }
        return None

    @classmethod
    def from_dict(cls, value: Any) -> "GoalState":
        if not isinstance(value, Mapping):
            raise ValueError("goal state must be an object")
        raw_gates = value.get("gates") or []
        raw_subgoals = value.get("subgoals") or []
        return cls(
            schema_version=value.get(
                "schema_version", value.get("schemaVersion", STATE_SCHEMA_VERSION)
            ),
            session_key=value.get("session_key", value.get("sessionKey", "")),
            goal_id=value.get("goal_id", value.get("goalId", "")),
            revision=value.get("revision", 0),
            conversation_epoch=value.get("conversation_epoch", value.get("conversationEpoch", 0)),
            goal=value.get("goal", ""),
            status=value.get("status", GoalStatus.ACTIVE.value),
            turns_used=value.get("turns_used", value.get("turnsUsed", 0)),
            max_turns=value.get("max_turns", value.get("maxTurns", DEFAULT_MAX_TURNS)),
            created_at=value.get("created_at", value.get("createdAt", 0.0)),
            updated_at=value.get("updated_at", value.get("updatedAt", 0.0)),
            last_turn_at=value.get("last_turn_at", value.get("lastTurnAt", 0.0)),
            last_verdict=value.get("last_verdict", value.get("lastVerdict")),
            last_reason=value.get("last_reason", value.get("lastReason")),
            paused_reason=value.get("paused_reason", value.get("pausedReason")),
            consecutive_parse_failures=value.get(
                "consecutive_parse_failures", value.get("consecutiveParseFailures", 0)
            ),
            consecutive_transport_failures=value.get(
                "consecutive_transport_failures", value.get("consecutiveTransportFailures", 0)
            ),
            consecutive_compaction_failures=value.get(
                "consecutive_compaction_failures",
                value.get("consecutiveCompactionFailures", 0),
            ),
            subgoals=raw_subgoals
            if isinstance(raw_subgoals, Iterable) and not isinstance(raw_subgoals, (str, bytes))
            else [],
            waiting_on_pid=value.get("waiting_on_pid", value.get("waitingOnPid")),
            waiting_on_session=value.get("waiting_on_session", value.get("waitingOnSession")),
            waiting_until=value.get("waiting_until", value.get("waitingUntil", 0.0)),
            waiting_reason=value.get("waiting_reason", value.get("waitingReason")),
            waiting_since=value.get("waiting_since", value.get("waitingSince", 0.0)),
            contract=GoalContract.from_dict(value.get("contract")),
            gates=[GoalGate.from_dict(item) for item in raw_gates if isinstance(item, Mapping)],
        )


def _clean_optional(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _clean_unique_strings(
    values: Iterable[Any],
    *,
    limit: int,
    max_items: int,
    field_name: str,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value, limit=limit, field_name=field_name)
        folded = text.casefold()
        if text and folded not in seen:
            result.append(text)
            seen.add(folded)
        if len(result) > max_items:
            raise ValueError(f"too many {field_name}s (maximum {max_items})")
    return result


@dataclass(frozen=True, slots=True)
class GoalDecision:
    status: GoalStatus | None
    verdict: GoalVerdict
    should_continue: bool = False
    continuation_prompt: str | None = None
    reason: str = ""
    message: str = ""
    goal_id: str | None = None
    revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value if self.status else None,
            "verdict": self.verdict.value,
            "shouldContinue": self.should_continue,
            "continuationPrompt": self.continuation_prompt,
            "reason": self.reason,
            "message": self.message,
            "goalId": self.goal_id,
            "revision": self.revision,
        }
