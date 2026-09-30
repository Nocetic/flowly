"""Recover canonical conversation identities without corrupting card IDs."""
from __future__ import annotations

import json
from pathlib import Path

from flowly.utils.helpers import safe_filename


def session_key_from_header(path: Path, header: dict) -> str:
    key = header.get('session_key')
    if key is not None:
        # ':' and '_' share a filename encoding. Retain the exact saved key,
        # but never let a damaged header redirect a listing to another file.
        if (not isinstance(key, str) or not key or len(key) > 512
                or any(char in key for char in ('/', '\\', '\x00'))
                or safe_filename(key.replace(':', '_')) + '.jsonl' != path.name):
            raise ValueError('Invalid stored session identity')
        return key
    if path.stem.startswith('desktop_voice-work_'):
        # Legacy work files contain generated IDs such as c_ab12. Only the
        # known namespace separators are encoded; preserve the card suffix.
        return 'desktop:voice-work:' + path.stem[len('desktop_voice-work_'):]
    return path.stem.replace('_', ':')


def read_session_header(path: Path) -> dict:
    with path.open(encoding='utf-8') as handle:
        line = handle.readline(2 * 1024 * 1024 + 1)
    if len(line) > 2 * 1024 * 1024:
        raise ValueError('Session metadata is too large')
    header = json.loads(line)
    if not isinstance(header, dict) or header.get('_type') != 'metadata':
        raise ValueError('Invalid session metadata')
    return header
