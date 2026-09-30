"""Canonical ownership checks shared by session storage and RPC readers."""
from __future__ import annotations

import json
from pathlib import Path

from flowly.live_voice.authority import RequestOwner, current_request_owner


class SessionAccessError(RuntimeError):
    code = 'NOT_FOUND'
    message = 'Conversation not found.'

    def __init__(self):
        super().__init__(self.message)


def owner_metadata(owner: RequestOwner) -> dict:
    return {'kind': 'account', 'uid': owner.uid} if owner.uid is not None else {'kind': 'host'}


def is_owned_session(key: str, metadata: dict) -> bool:
    return ('voiceOwner' in metadata
            or key.replace('_', ':').startswith(('desktop:voice:', 'desktop:voice-work:')))


def session_visible(key: str, metadata: dict) -> bool:
    owner = current_request_owner()
    if owner is None:
        return True
    if not isinstance(metadata, dict):
        return False
    if not is_owned_session(key, metadata):
        return True
    return metadata.get('voiceOwner', {'kind': 'host'}) == owner_metadata(owner)


def require_session_access(key: str, metadata: dict) -> None:
    if not session_visible(key, metadata):
        raise SessionAccessError()


def read_session_metadata(path: Path, key: str, *, allow_missing_work: bool = False) -> dict | None:
    """Read canonical authority metadata; this does not authorize its caller."""
    try:
        with path.open(encoding='utf-8') as handle:
            line = handle.readline(2 * 1024 * 1024 + 1)
        if len(line) > 2 * 1024 * 1024:
            raise SessionAccessError()
        row = json.loads(line)
        if not isinstance(row, dict) or row.get('_type') != 'metadata' or not isinstance(row.get('metadata'), dict):
            raise SessionAccessError()
        metadata = row['metadata']
    except FileNotFoundError:
        if path.with_name(path.stem + '.full.jsonl').exists():
            raise SessionAccessError() from None
        # A client's first write is not authority for a dispatcher-owned task.
        # Only the private reservation operation may establish its owner.
        if not allow_missing_work and key.replace('_', ':').startswith('desktop:voice-work:'):
            raise SessionAccessError() from None
        return None
    except (OSError, ValueError, UnicodeError):
        raise SessionAccessError() from None
    return metadata


def require_session_file(path: Path, key: str, *, allow_missing_work: bool = False) -> dict | None:
    """Authorize canonical storage, never a remaining archive or cached object."""
    if current_request_owner() is None:
        return None
    metadata = read_session_metadata(path, key, allow_missing_work=allow_missing_work)
    if metadata is not None:
        require_session_access(key, metadata)
    return metadata


def require_rpc_session(method: str, params: dict, *, sessions_dir: Path | None = None) -> None:
    """Check an explicit chat target before acceptance or other side effects."""
    if current_request_owner() is None or not isinstance(method, str) or not method.startswith(('chat.', 'sessions.', 'goal.')):
        return
    key = params.get('sessionKey') or params.get('key')
    if not key:
        return
    if not isinstance(key, str) or len(key) > 512 or any(char in key for char in ('/', '\\', '\x00')):
        raise SessionAccessError()
    from flowly.profile import get_flowly_home
    from flowly.utils.helpers import safe_filename

    directory = sessions_dir if sessions_dir is not None else get_flowly_home() / 'sessions'
    require_session_file(directory / (safe_filename(key.replace(':', '_')) + '.jsonl'), key)
