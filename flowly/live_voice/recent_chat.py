"""The owner's latest conversation with the agent, for a call to continue it.

A chat turn carries its own history, so the agent always knows what was just
said. A call is a separate conversation: without this it knew the owner's
memory but not what the two had been talking about a minute earlier. The
call's memory snapshot therefore carries the end of the conversation the
owner was most recently in (any chat channel; never another call, a call's
work, an agent room or a scheduled run), visible to the requester exactly as
``sessions.list`` would show it.

Read-only. Bounded: the newest user and assistant messages, at most
``RECENT_CHAT_MESSAGES`` and ``RECENT_CHAT_BYTES`` UTF-8 bytes, each cut at
``MESSAGE_CHARS``, read from at most the file's last ``TAIL_BYTES``; tool
calls and tool output are left out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

RECENT_CHAT_MESSAGES = 12
RECENT_CHAT_BYTES = 6_000
MESSAGE_CHARS = 600
TAIL_BYTES = 512 * 1024
CANDIDATES = 24

# Conversations the owner has with the agent, by session key.
CHAT_PREFIXES = ('desktop:', 'web:', 'ios:', 'android:', 'telegram:', 'whatsapp:', 'discord:', 'slack:', 'imessage:')
# Inside those, what is not a conversation with the owner.
NOT_CONVERSATIONS = ('desktop:voice:', 'desktop:voice-work:', 'desktop:profile-', 'desktop:group:')


@dataclass(frozen=True)
class RecentChat:
    key: str
    title: str
    updated_at: datetime
    text: str


def is_owner_chat(key: str) -> bool:
    return key.startswith(CHAT_PREFIXES) and not key.startswith(NOT_CONVERSATIONS)


def latest_chat(sessions_dir: Path) -> RecentChat | None:
    """The most recently active owner conversation the requester may see,
    as ``User:``/``Agent:`` lines; None when there is none."""
    from flowly.session.keys import read_session_header, session_key_from_header
    from flowly.session.manager import iter_session_files
    from flowly.session.ownership import session_visible

    if not sessions_dir.is_dir():
        return None
    files = sorted(iter_session_files(sessions_dir), key=_mtime, reverse=True)
    for path in files[:CANDIDATES]:
        try:
            header = read_session_header(path)
            key = session_key_from_header(path, header)
            metadata = header.get('metadata') if isinstance(header.get('metadata'), dict) else {}
        except (OSError, ValueError):
            continue
        if not is_owner_chat(key) or not session_visible(key, metadata):
            continue
        lines = _tail_messages(path)
        if not lines:
            continue
        title = str(metadata.get('title') or '').strip()
        return RecentChat(key=key, title=' '.join(title.split())[:120], text='\n\n'.join(lines),
                          updated_at=datetime.fromtimestamp(_mtime(path)))
    return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _tail_messages(path: Path) -> list[str]:
    try:
        with path.open('rb') as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    rows = raw.decode('utf-8', 'ignore').splitlines()
    if size > TAIL_BYTES and rows:
        rows = rows[1:]  # the read began inside a line
    picked: list[str] = []
    left = RECENT_CHAT_BYTES
    for row in reversed(rows):
        try:
            message = json.loads(row)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
            continue
        text = _text(message.get('content'))
        if not text:
            continue  # an assistant turn that only called tools
        speaker = 'User' if message['role'] == 'user' else 'Agent'
        if len(text) > MESSAGE_CHARS:
            text = text[:MESSAGE_CHARS].rsplit(' ', 1)[0] + ' …'
        line = f'{speaker}: {text}'
        left -= len(line.encode()) + 2
        if left < 0 and picked:
            break  # the newest messages are kept
        picked.append(line)
        if len(picked) == RECENT_CHAT_MESSAGES:
            break
    return list(reversed(picked))


def _text(content) -> str:
    if isinstance(content, list):
        content = ' '.join(part.get('text', '') for part in content
                           if isinstance(part, dict) and part.get('type') == 'text')
    return ' '.join(content.split()) if isinstance(content, str) else ''
