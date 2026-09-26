"""Bind live controls to their original session and its canonical owner."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    VoiceAuthorityError,
    current_request_owner,
)
from flowly.session.ownership import (
    SessionAccessError,
    is_owned_session,
    owner_metadata,
    read_session_metadata,
    require_session_access,
    require_session_file,
)
from flowly.utils.helpers import safe_filename


@dataclass(frozen=True)
class SessionControlScope:
    key: str | None
    path: Path | None
    owner: dict | None

    @classmethod
    def bind(cls, key: str | None, owner: dict | None, *, sessions_dir: Path | None = None):
        if owner is not None:
            try:
                if owner == {'kind': 'host'}:
                    owner = dict(owner)
                elif (isinstance(owner, dict) and set(owner) == {'kind', 'uid'}
                      and owner['kind'] == 'account' and owner['uid'] is not None):
                    owner = owner_metadata(RequestOwner(owner['uid']))
                else:
                    raise SessionAccessError()
            except VoiceAuthorityError:
                raise SessionAccessError() from None
        if key is None:
            return cls(None, None, owner)
        if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(c in key for c in ('/', '\\', '\x00')):
            raise SessionAccessError()
        from flowly.profile import get_flowly_home

        directory = sessions_dir if sessions_dir is not None else get_flowly_home() / 'sessions'
        return cls(key, directory / (safe_filename(key.replace(':', '_')) + '.jsonl'), owner)

    @classmethod
    def capture(cls, key: str | None, *, sessions_dir: Path | None = None):
        if not key:
            return cls.bind(None, owner_metadata(current_request_owner() or HOST_OWNER))
        scope = cls.bind(key, None, sessions_dir=sessions_dir)
        metadata = read_session_metadata(scope.path, key, allow_missing_work=True) or {}
        require_session_access(key, metadata)
        owner = metadata.get('voiceOwner', {'kind': 'host'}) if is_owned_session(key, metadata) else None
        if is_owned_session(key, metadata) and owner is None:
            raise SessionAccessError()
        return cls.bind(key, owner, sessions_dir=scope.path.parent)

    @contextmanager
    def guard(self, key: str | None) -> Iterator[bool]:
        owner = current_request_owner()
        if owner is None:
            yield True
            return
        if key != self.key or (self.owner is not None and self.owner != owner_metadata(owner)):
            yield False
            return
        if self.path is None:
            yield True
            return
        from flowly.session.manager import session_file_lock

        with session_file_lock(self.path):
            try:
                canonical = require_session_file(self.path, self.key)
                allowed = (not is_owned_session(self.key, canonical or {}) if self.owner is None
                           else canonical is not None
                           and canonical.get('voiceOwner', {'kind': 'host'}) == self.owner)
            except SessionAccessError:
                allowed = False
            # Hold the canonical lock through the synchronous Future/control
            # mutation, so deletion/recreation cannot change its owner midway.
            yield allowed


@contextmanager
def pending_control_guard(scopes: dict, pending) -> Iterator[bool]:
    if current_request_owner() is None:
        yield True
        return
    scope = scopes.get(pending.id)
    if scope is None:
        # Legacy manually registered shared requests have no private binding.
        # Never infer an owned request's original principal from its reader.
        if not pending.session_key or is_owned_session(pending.session_key, {}):
            yield False
            return
        scope = SessionControlScope.bind(pending.session_key, None)
    with scope.guard(pending.session_key) as allowed:
        yield allowed


@contextmanager
def run_control_guard(commands, params: dict, *, sessions_dir: Path | None = None) -> Iterator[str | None]:
    if current_request_owner() is None:
        yield params.get('sessionKey')
        return
    from flowly.agent import inflight

    run_id = params.get('runId')
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 512 or any(ord(c) < 32 for c in run_id):
        raise SessionAccessError()
    accepted = commands.control_scope(run_id, sessions_dir=sessions_dir)
    live = inflight.control_scope(run_id)
    if accepted and live and accepted.key != live.key:
        raise SessionAccessError()
    scope = live or accepted
    supplied = params.get('sessionKey')
    if scope is None or (supplied is not None and supplied != scope.key):
        raise SessionAccessError()
    with scope.guard(scope.key) as allowed:
        if not allowed:
            raise SessionAccessError()
        # If both records exist their original owners must also agree.
        if accepted and live and accepted.owner != live.owner:
            raise SessionAccessError()
        yield scope.key
