"""Causal run identity for goals created during a serialized agent turn."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass
class _Turn:
    session_key: str
    run_id: str
    active: bool = True


_TURN: ContextVar[_Turn | None] = ContextVar('goal_creation_turn', default=None)


@contextmanager
def goal_turn_scope(session_key: str, run_id: str) -> Iterator[None]:
    scope = _Turn(session_key, run_id)
    token = _TURN.set(scope)
    try:
        yield
    finally:
        # Child tasks inherit context values. Revoke their evidence when the
        # serialized turn ends, even if a background task outlives its parent.
        scope.active = False
        _TURN.reset(token)


def creating_run_id(session_key: str) -> str | None:
    current = _TURN.get()
    return current.run_id or None if current and current.active and current.session_key == session_key else None
