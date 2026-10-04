"""Past conversations, for a call's recall (``voice.context``).

The agent's chat turns can search every past conversation (``session_search``);
a call's recall searched only memory, so "what did we say about X last week?"
went to a separate task and the owner waited. Recall now also searches the
same conversation index, read-only, and returns the best matching moment of
at most ``RESULT_CONVERSATIONS`` conversations, each with the messages around
it.

Only conversations with the owner are searched (chats on any channel, and
calls; never a call's work, an agent room or a scheduled run), only while
their session still exists, and only as ``sessions.list`` would show them to
the requester: another account's calls never. Governance withholding and
secret redaction are applied by the caller as for every recalled fact.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

RESULT_CONVERSATIONS = 3
CONTEXT_MESSAGES = 2
LINE_CHARS = 240
CANDIDATE_MESSAGES = 40
STEM = 5
_STATES = ('active', 'compacted')


@dataclass(frozen=True)
class ConversationHit:
    key: str
    title: str
    at: float
    text: str
    is_call: bool


def search_conversations(index_path: Path, sessions_dir: Path, query: str) -> list[ConversationHit]:
    """The best matching moments in past conversations for ``query``.

    Every word must match first (as ``session_search`` does). A spoken query
    rarely repeats a whole phrase ("what did we say about the trip"), so with
    no such match a message may match fewer words, weighted by how rare each
    is among all messages: it must carry at least half of the query's weight,
    so "what", "about" or "last week" cannot bring back an unrelated
    conversation while "penicillin" can. Words of four letters or more are
    compared by their first five letters, as a rough stem: Turkish adds its
    endings to the word ("alerjim", "alerjisi"), English mostly does too.
    """
    from flowly.live_voice.recent_chat import CALL_PREFIX, is_owner_chat
    from flowly.session.indexer import _sanitize_fts5_query

    strict = _sanitize_fts5_query(query)
    words = list(dict.fromkeys(word.casefold()[:STEM] for word in re.findall(r'\w+', query) if len(word) >= 4))
    loose = ' OR '.join(f'{word}*' for word in words)
    if not strict or not index_path.exists() or not sessions_dir.is_dir():
        return []
    connection = sqlite3.connect(f'{index_path.as_uri()}?mode=ro', uri=True, timeout=0.5)
    connection.row_factory = sqlite3.Row
    try:
        rows = _matches(connection, strict)
        if not rows and loose:
            weights = _rarity(connection, words)
            total = sum(weights.values())
            rows = [row for row in _matches(connection, loose)
                    if total and sum(weight for word, weight in weights.items()
                                     if _has_word(row['content'], word)) >= total / 2]
        hits: list[ConversationHit] = []
        seen: set[str] = set()
        for row in rows:
            key = row['session_key']
            if key in seen or not is_owner_chat(key):
                continue
            seen.add(key)
            title = _visible_title(sessions_dir, key)
            if title is None:
                continue
            lines = _around(connection, key, row['id'])
            if not lines:
                continue
            hits.append(ConversationHit(key=key, title=title, at=float(row['timestamp'] or 0),
                                        text='\n'.join(lines), is_call=key.startswith(CALL_PREFIX)))
            if len(hits) == RESULT_CONVERSATIONS:
                break
        return hits
    finally:
        connection.close()


def _matches(connection: sqlite3.Connection, match: str) -> list[sqlite3.Row]:
    try:
        return connection.execute(
            'SELECT m.id, m.session_key, m.timestamp, m.content FROM messages_fts '
            'JOIN messages m ON m.id = messages_fts.rowid '
            "WHERE messages_fts MATCH ? AND m.state IN (?, ?) AND m.role IN ('user', 'assistant') "
            'ORDER BY bm25(messages_fts) LIMIT ?',
            (match, *_STATES, CANDIDATE_MESSAGES)).fetchall()
    except sqlite3.OperationalError:
        return []


def _rarity(connection: sqlite3.Connection, words: list[str]) -> dict[str, float]:
    """How much each word tells: log(messages / messages containing it)."""
    total = connection.execute('SELECT count(*) FROM messages').fetchone()[0] or 1
    weights = {}
    for word in words:
        try:
            found = connection.execute('SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?',
                                       (f'{word}*',)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        # A word no message has weighs most: the topic asked about is not there.
        weights[word] = math.log((total + 1) / (found + 1))
    return weights


def _has_word(content: str, word: str) -> bool:
    """A word or a longer form of it (Turkish suffixes: tren → trene)."""
    return any(token.startswith(word) for token in re.findall(r'\w+', str(content or '').casefold()))


def _around(connection: sqlite3.Connection, key: str, anchor: int) -> list[str]:
    """The matching message with up to ``CONTEXT_MESSAGES`` spoken or written
    messages on each side, from the same conversation (ids are shared by
    every conversation, so neighbours are found per conversation)."""
    spoken = "AND role IN ('user', 'assistant') AND state IN (?, ?) AND trim(content) != ''"
    center = connection.execute(f'SELECT id, role, content FROM messages WHERE id = ? {spoken}',
                                (anchor, *_STATES)).fetchall()
    if not center:
        return []
    before = connection.execute(
        f'SELECT id, role, content FROM messages WHERE session_key = ? AND id < ? {spoken} ORDER BY id DESC LIMIT ?',
        (key, anchor, *_STATES, CONTEXT_MESSAGES)).fetchall()
    after = connection.execute(
        f'SELECT id, role, content FROM messages WHERE session_key = ? AND id > ? {spoken} ORDER BY id LIMIT ?',
        (key, anchor, *_STATES, CONTEXT_MESSAGES)).fetchall()
    window = [*reversed(before), *center, *after]
    lines = []
    for row in window:
        text = ' '.join(str(row['content']).split())
        if len(text) > LINE_CHARS:
            text = text[:LINE_CHARS].rsplit(' ', 1)[0] + ' …'
        lines.append(f"{'User' if row['role'] == 'user' else 'Agent'}: {text}")
    return lines


def _visible_title(sessions_dir: Path, key: str) -> str | None:
    """The conversation's title when its session still exists and the
    requester may see it; None otherwise."""
    from flowly.session.keys import read_session_header, session_key_from_header
    from flowly.session.ownership import session_visible
    from flowly.utils.helpers import safe_filename

    path = sessions_dir / (safe_filename(key.replace(':', '_')) + '.jsonl')
    try:
        header = read_session_header(path)
        if session_key_from_header(path, header) != key:
            return None
    except (OSError, ValueError):
        return None
    metadata = header.get('metadata') if isinstance(header.get('metadata'), dict) else {}
    if not session_visible(key, metadata):
        return None
    return ' '.join(str(metadata.get('title') or '').split())[:120]
