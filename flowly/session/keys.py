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


# ── Subagent threads ────────────────────────────────────────────────────────
#
# A subagent's conversation (``subagent:<threadId>``) is written only by the
# agent that started it, through its own run (flowly.agent.subagent). Every
# client, the owner's included, may read it and never write to it: a message,
# a steer, a compaction, a model change or a deletion naming one is refused
# with SUBAGENT_THREAD_READ_ONLY on every path (direct gateway, relay, profile
# host). Only the methods below may name one, and they only read.

SUBAGENT_THREAD_PREFIX = 'subagent:'
SUBAGENT_THREAD_READ_ONLY = 'SUBAGENT_THREAD_READ_ONLY'
SUBAGENT_THREAD_READ_ONLY_MESSAGE = (
    "A subagent's conversation is read-only: only the agent that started it writes to it."
)
SUBAGENT_THREAD_READABLE_METHODS = frozenset({
    'chat.history',
    'chat.inflight',
    'subagents.history',
    # What a chat view reads beside its history; never a write.
    'goal.get',
    'plan.get',
    'plan.list',
    'plan.mode.get',
})


class SubagentThreadReadOnlyError(RuntimeError):
    code = SUBAGENT_THREAD_READ_ONLY
    message = SUBAGENT_THREAD_READ_ONLY_MESSAGE

    def __init__(self):
        super().__init__(self.message)


def is_subagent_thread(key: object) -> bool:
    """Whether ``key`` names a subagent's conversation."""
    return isinstance(key, str) and key.strip().startswith(SUBAGENT_THREAD_PREFIX)


def require_writable_conversation(key: object) -> None:
    """Refuse a write to a subagent's conversation."""
    if is_subagent_thread(key):
        raise SubagentThreadReadOnlyError()


def rpc_session_keys(method: object, params: object) -> list[str]:
    """The conversations an RPC names (``sessionKey``; ``key`` for chat, session and goal calls)."""
    if not isinstance(params, dict):
        return []
    keys = [params.get('sessionKey')]
    if isinstance(method, str) and method.startswith(('chat.', 'sessions.', 'goal.')):
        keys.append(params.get('key'))
    return [key for key in keys if isinstance(key, str)]


def require_rpc_subagent_access(method: object, params: object) -> None:
    """Refuse any RPC but a read that names a subagent's conversation."""
    if method in SUBAGENT_THREAD_READABLE_METHODS:
        return
    for key in rpc_session_keys(method, params):
        require_writable_conversation(key)
