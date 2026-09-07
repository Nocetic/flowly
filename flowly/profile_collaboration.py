"""In-process authority for one profile collaboration turn.

This object is never accepted from RPC parameters or persisted in a transcript.
Transports and the host mint it; the agent binds it at the execution boundary,
including when an inbound message has crossed the asynchronous message bus.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

ProfileBroker = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class _RunLease:
    active: bool = True


@dataclass(frozen=True, slots=True)
class ProfileRunBinding:
    client_id: str
    session_key: str
    current_profile: str
    available_profiles: tuple[str, ...]
    correlation_id: str
    hop: int = 0
    turn_origin: str = "user"
    run_id: str = ""
    broker: ProfileBroker | None = field(default=None, repr=False, compare=False)
    _lease: _RunLease = field(default_factory=_RunLease, repr=False, compare=False)

    @property
    def is_active(self) -> bool:
        return self._lease.active

    def close(self) -> None:
        # asyncio children copy context variables. Revocation prevents an
        # abandoned child from using its parent's authority after turn end.
        self._lease.active = False

    def metadata(self) -> dict[str, Any]:
        from flowly.profile import profile_display_names

        return {
            "profile_current": self.current_profile,
            "profile_directory": list(self.available_profiles),
            "profile_display_names": profile_display_names(self.available_profiles),
            "profile_correlation_id": self.correlation_id,
            "profile_hop": self.hop,
            "turn_origin": self.turn_origin,
        }


PROFILE_RUN_BINDING: ContextVar[ProfileRunBinding | None] = ContextVar(
    "flowly_profile_run_binding", default=None,
)
