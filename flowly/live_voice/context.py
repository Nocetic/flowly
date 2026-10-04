"""Bounded, sourced recall for the current runtime, with governance filtering.

No caller-selected filesystem path, profile, credentials, tools, or system
prompt is accepted. The authenticated profile host chooses the runtime.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from flowly.compaction.redaction import redact_secrets
from flowly.live_voice.conversation_recall import search_conversations
from flowly.live_voice.memory_view import GovernedMemory, read_db, read_memory_file, revision_of
from flowly.live_voice.sessions import VoiceError, bounded_text, integer
from flowly.memory.summary import SENTINEL_END, SENTINEL_START


def validate_context(params: dict) -> dict:
    if set(params) - {'query', 'limit'}:
        raise VoiceError('INVALID_PARAMS', 'Context accepts only query and limit; the runtime owns its scope.')
    return {'query': bounded_text(params.get('query', ''), 'query', maximum=500, empty=True),
            'limit': integer(params.get('limit', 8), 'limit', minimum=1, maximum=12)}


_revision = revision_of
_read_db = read_db


def manual_memory(text: str) -> str:
    """MEMORY.md without its generated block(s): the human-written notes.

    The generated block renders governed items; callers take those from the
    governance view instead, so a withdrawn item is never read back from it.
    """
    return _manual(text)


def _manual(text: str) -> str:
    # An unfinished generated block is also excluded: it may contain material
    # withdrawn from governance while a writer is regenerating the file.
    return re.sub(re.escape(SENTINEL_START) + r'.*?(?:' + re.escape(SENTINEL_END) + r'|\Z)', '', text, flags=re.S)


class VoiceContext:
    def __init__(self, workspace: Path, *, state_db: Callable[[str], Path],
                 profile: Callable[[], tuple[str, str]], index: Callable[[], Any] = lambda: None,
                 conversations: Callable[[], tuple[Path, Path] | None] = lambda: None):
        self.workspace = workspace.resolve()
        self.state_db = state_db
        self.profile = profile
        self.index = index
        # (conversation index, sessions folder): past conversations recall
        # searches as the agent's session_search does.
        self.conversations = conversations

    async def search(self, params: dict) -> dict:
        request = validate_context(params)
        query, limit = request['query'], request['limit']
        scope_profile, bot_id = self.profile()
        words = set(re.findall(r'\w+', query.casefold()))
        facts: list[dict] = []
        sources: dict[str, dict] = {}
        now = datetime.now(timezone.utc).isoformat()

        def rank(text: str) -> int:
            folded = text.casefold()
            return sum(word in folded for word in words) if words else 1

        searched: set[int] = set()

        def add(text: str, ref: str, revision: str, updated_at: Any = None, *, matched: bool = False,
                **provenance) -> None:
            text = redact_secrets(text).strip()
            # A memory-search match is relevant by the agent's own search (as
            # in its chat), even without the query's literal words.
            if not text or (words and not matched and not rank(text)):
                return
            if matched:
                searched.add(len(facts))
            facts.append({'text': text[:900], 'sourceRef': ref, 'revision': revision,
                          'updatedAt': updated_at, **provenance})

        governed = GovernedMemory.load(self.state_db, now)
        governance_ok = governed.available
        protected, excluded_triples = governed.protected, governed.excluded_triples
        if governance_ok:
            try:
                for item in governed.allowed:
                    add(item['text'], f"memory://item/{quote(item['id'], safe='')}", _revision(item),
                        item.get('updated_at'), sourceSession=item['source_session'],
                        sourceMessageIds=json.loads(item['source_message_ids']))
            except (ValueError, KeyError, TypeError):
                # A malformed governed row is an unreadable index: export nothing.
                governance_ok = False
                facts = []
        sources['governance'] = ({'status': governed.status, 'revision': governed.revision}
                                 if governance_ok else {'status': 'unavailable'})

        def read_file(relative: str) -> tuple[str, str, str] | None:
            source = read_memory_file(self.workspace, relative)
            if source is None:
                return None
            text, revision, updated_at = source
            # A manually copied governed item does not bypass its privacy or
            # lifecycle. Conservatively withhold that file if it contains one.
            if governed.withholds(text):
                return None
            return _manual(text), revision, updated_at

        if governance_ok:
            for name, relative in [('user', 'USER.md'), ('memory', 'memory/MEMORY.md')]:
                try:
                    source = read_file(relative)
                    if source:
                        text, revision, updated_at = source
                        chunks = re.split(r'\n\s*\n|<!--.*?-->', text)
                        for position, chunk in enumerate(chunks):
                            add(chunk, f'memory://file/{relative}#section-{position}', revision, updated_at)
                        sources[name] = {'status': 'ok', 'revision': revision}
                    else:
                        sources[name] = {'status': 'empty'}
                except (OSError, ValueError):
                    sources[name] = {'status': 'unavailable'}

            try:
                index = self.index()
                matches = await asyncio.wait_for(
                    index.search(query, max_results=12, embedding_timeout=0.8), 1.5
                ) if query and index else []
                for match in matches:
                    relative = str(match.path)
                    # Search results are data. Do not follow an index entry
                    # into another profile, a session archive, or a symlink.
                    if not relative.startswith('memory/') or not relative.endswith('.md'):
                        continue
                    source = read_file(relative)
                    if not source:
                        continue
                    text, revision, updated_at = source
                    # Verify the snippet against current manual source content;
                    # stale/generated index entries cannot restore removed facts.
                    # Display snippets may end with a synthetic ellipsis. Use
                    # full evidence for verification, then bound the exported
                    # fact in add(); never loosen the stale/privacy check.
                    snippet = str(getattr(match, 'source_text', '') or match.snippet).strip()
                    if snippet and snippet in text:
                        add(snippet, f'memory://file/{quote(relative, safe="/")}#L{match.start_line}', revision,
                            updated_at, matched=True, startLine=match.start_line, endLine=match.end_line)
                sources['search'] = {'status': 'ok' if index else 'unavailable'}
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                sources['search'] = {'status': 'unavailable'}

            try:
                path = self.state_db('knowledge_graph.sqlite3')
                triples = []
                if path.exists():
                    # Limit at the database before constructing the model projection.
                    terms = sorted(words)[:8]
                    where = ' AND (' + ' OR '.join('(s.name || t.predicate || o.name) LIKE ?' for _ in terms) + ')' if terms else ''
                    triples = _read_db(path,
                        'SELECT t.id, s.name AS subject, t.predicate, o.name AS object, t.valid_from, t.valid_to '
                        'FROM triples t JOIN entities s ON t.subject=s.id JOIN entities o ON t.object=o.id '
                        "WHERE (t.valid_to IS NULL OR t.valid_to > ?) AND (t.valid_from IS NULL OR t.valid_from <= ?)" + where +
                        ' ORDER BY t.id DESC LIMIT 100', (now, now, *(f'%{w}%' for w in terms)))
                for triple in triples:
                    if str(triple['id']) in excluded_triples:
                        continue
                    text = f"{triple['subject']} {triple['predicate']} {triple['object']}"
                    if not any(item in text.casefold() for item in protected):
                        add(text, f"memory://relation/{triple['id']}", _revision(triple), validFrom=triple['valid_from'])
                sources['knowledge'] = {'status': 'ok' if triples else 'empty', 'revision': _revision(triples)}
            except (OSError, sqlite3.Error, ValueError):
                sources['knowledge'] = {'status': 'unavailable'}

            # What was said: the best matching moments of past conversations
            # (chats and calls), as the agent's own session_search finds them.
            located = self.conversations() if query else None
            if located:
                try:
                    hits = await asyncio.wait_for(asyncio.to_thread(search_conversations, *located, query), 1.5)
                    for hit in hits:
                        if governed.withholds(hit.text):
                            continue
                        heading = f"{'Call' if hit.is_call else 'Conversation'}{': ' + hit.title if hit.title else ''}"
                        when = datetime.fromtimestamp(hit.at, timezone.utc).isoformat() if hit.at else None
                        add(f"{heading} ({when[:10] if when else 'earlier'})\n{hit.text}",
                            f"memory://conversation/{_revision(hit.key)}", _revision(hit.text), when,
                            matched=True, kind='conversation')
                    sources['conversations'] = {'status': 'ok' if hits else 'empty'}
                except (OSError, sqlite3.Error, ValueError, asyncio.TimeoutError):
                    sources['conversations'] = {'status': 'unavailable'}
        else:
            # A corrupt/unreadable privacy index is not equivalent to no
            # private items. Do not export unfiltered mirrors as a fallback.
            sources.update({name: {'status': 'unavailable'} for name in ('user', 'memory', 'search', 'knowledge')})

        unique: dict[str, dict] = {}
        # Keyword overlap ranks; a memory-search match counts one more, so a
        # semantic hit is not pushed out by facts that merely share a word.
        order = sorted(range(len(facts)), key=lambda i: -(rank(facts[i]['text']) + (i in searched)))
        for fact in (facts[i] for i in order):
            unique.setdefault(fact['text'].casefold(), fact)
        selected = list(unique.values())[:limit]
        return {'scope': {'profile': scope_profile, 'botId': bot_id}, 'facts': selected, 'sources': sources,
                'partial': any(source['status'] == 'unavailable' for source in sources.values()),
                'revision': _revision({'sources': sources, 'facts': selected}),
                'contentRole': 'reference_data'}
