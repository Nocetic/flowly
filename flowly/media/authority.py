"""Durable publication authority for private media, independent of the index.

Private names remain reserved when their sidecar is lost. A directory scan
must never turn missing provenance into public ownership. The original scope
and the published inode are written before any visible bytes are linked in.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

from flowly.live_voice.authority import RequestOwner, VoiceAuthorityError, current_request_owner
from flowly.live_voice.events import EventAccess, current_event_access
from flowly.session.control_access import SessionControlScope
from flowly.session.ownership import SessionAccessError, owner_metadata

PRIVATE_PREFIX = 'voice-private-'
_DIRECTORY = '.authority'
_MAX_RECORD = 65536


def capture_media_access() -> EventAccess:
    """Capture before provider I/O; never adopt the eventual reader's scope."""
    source = current_event_access() or EventAccess.capture()
    access = EventAccess(
        scopes=tuple(SessionControlScope.bind(scope.key, scope.owner,
                     sessions_dir=scope.path.parent if scope.path else None) for scope in source.scopes),
        owner=source.owner, blocked=source.blocked, canonical=source.canonical,
    )
    _require_producer(access)
    return access


def _require_producer(access: EventAccess) -> None:
    producer = access.producer()
    caller = current_request_owner()
    if (not access.canonical or not access.permits(producer)
            or (caller is not None and not access.permits(caller))):
        raise SessionAccessError()


def _private(access: EventAccess) -> bool:
    return access.owner is not None or any(scope.owner is not None for scope in access.scopes)


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _sync_directory(path: Path) -> None:
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_media_bytes(data: bytes, destination: Path, *, access: EventAccess | None = None) -> Path:
    """Use the same durable publication boundary for uploads and byte providers."""
    access = capture_media_access() if access is None else access
    _require_producer(access)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f'.publish-{uuid.uuid4().hex}.part'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
        return publish_media_file(temporary, destination, access)
    finally:
        temporary.unlink(missing_ok=True)


def publish_media_file(temporary: Path, destination: Path, access: EventAccess) -> Path:
    """Publish a completed hidden file without overwriting an existing output.

    The caller owns temporary-file cleanup on failure. A private destination
    receives an opaque reserved prefix; callers must use the returned path.
    """
    _require_producer(access)
    root = destination.parent.resolve()
    if (temporary.parent.resolve() != root or not temporary.name.startswith('.')
            or temporary.is_symlink() or not stat.S_ISREG(temporary.stat().st_mode)
            or destination.name.startswith('.') or '..' in destination.name
            or any(char in destination.name for char in ('\\', '\x00'))):
        raise SessionAccessError()
    destination = root / destination.name
    private = _private(access)
    if private and not destination.name.startswith(PRIVATE_PREFIX):
        destination = destination.with_name(PRIVATE_PREFIX + destination.name)
    if not private and destination.name.startswith(PRIVATE_PREFIX):
        raise SessionAccessError()
    record = None
    with temporary.open('rb') as handle:
        os.fsync(handle.fileno())
        identity = _identity(os.fstat(handle.fileno()))
    if private:
        sessions = root.parent / 'sessions'
        for scope in access.scopes:
            expected = SessionControlScope.bind(scope.key, scope.owner, sessions_dir=sessions)
            if scope.path != expected.path:
                raise SessionAccessError()
        directory = root / _DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise SessionAccessError()
        payload = {
            'version': 1, 'mediaId': destination.name, 'identity': identity,
            'owner': owner_metadata(access.owner) if access.owner is not None else None,
            'scopes': [{'key': scope.key, 'owner': scope.owner} for scope in access.scopes],
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(encoded) > _MAX_RECORD or len(access.scopes) > 8:
            raise SessionAccessError()
        record = directory / (destination.name + '.json')
        descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(directory)
            _sync_directory(root)
        except BaseException:
            record.unlink(missing_ok=True)
            raise
    try:
        # Hard linking, rather than replace(), makes name collisions fail.
        _require_producer(access)
        os.link(temporary, destination)
    except BaseException:
        if record is not None:
            record.unlink(missing_ok=True)
        raise
    temporary.unlink()
    _sync_directory(root)
    return destination


def media_visible(path: Path, *, allow_missing: bool = False) -> bool:
    """Authorize both an alias and its resolved target; missing authority denies."""
    try:
        target = path.resolve()
        for candidate in {path, target}:
            if not candidate.name.startswith(PRIVATE_PREFIX):
                continue
            directory = candidate.parent / _DIRECTORY
            record = directory / (candidate.name + '.json')
            if directory.is_symlink() or record.is_symlink():
                return False
            with record.open('rb') as handle:
                encoded = handle.read(_MAX_RECORD + 1)
            if len(encoded) > _MAX_RECORD:
                return False
            data = json.loads(encoded)
            if (not isinstance(data, dict)
                    or set(data) != {'version', 'mediaId', 'identity', 'owner', 'scopes'}
                    or type(data['version']) is not int or data['version'] != 1
                    or data['mediaId'] != candidate.name
                    or not isinstance(data['identity'], list) or len(data['identity']) != 4
                    or any(type(value) is not int or value < 0 for value in data['identity'])
                    or not isinstance(data['scopes'], list) or len(data['scopes']) > 8):
                return False
            scopes = []
            for item in data['scopes']:
                if not isinstance(item, dict) or set(item) != {'key', 'owner'}:
                    return False
                scopes.append(SessionControlScope.bind(item['key'], item['owner'],
                              sessions_dir=candidate.parent.parent / 'sessions'))
            owner = SessionControlScope.bind(None, data['owner']).owner
            access = EventAccess(scopes=tuple(scopes),
                                 owner=RequestOwner(owner.get('uid')) if owner is not None else None)
            if not _private(access) or not access.permits(current_request_owner() or access.producer()):
                return False
            try:
                if _identity(candidate.stat()) != data['identity']:
                    return False
            except FileNotFoundError:
                if not allow_missing:
                    return False
        return True
    except (OSError, RuntimeError, ValueError, TypeError, SessionAccessError, VoiceAuthorityError):
        return False
