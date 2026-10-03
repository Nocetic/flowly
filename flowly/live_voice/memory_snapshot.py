"""The agent's memory and identity for a voice call: the same knowledge the
agent's own chat turns carry, minus what may not leave the agent.

A chat turn's system prompt carries the agent's identity files (SOUL.md plus
an active persona, IDENTITY.md, USER.md), its memory (the governed items as
MEMORY.md renders them, the knowledge-graph summary, the human-written notes)
and, when the agent has no memory search, its last days of notes. A voice
call's backend gets exactly those sources, built the same way: the governed
items go through the same renderer (``render_generated_block``), the KG
through the same summary, files through the same prompt-injection scan. What
differs is governance: only items a call may see (``GovernedMemory``), and a
paragraph that carries a withheld item's words is left out of every file.

The speaking model, which must answer within a breath, gets a short profile
from the same sources: who the agent is and who the user is.

Read-only and side-effect free. An unreadable governance index exports
nothing (fail closed). ``knownRevision`` lets a call refresh cheaply: when
nothing changed the reply carries only the revision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from flowly.compaction.redaction import redact_secrets
from flowly.live_voice.context import manual_memory
from flowly.live_voice.memory_view import GovernedMemory, read_memory_file, revision_of
from flowly.live_voice.sessions import VoiceError

# Characters of section text a call's backend receives: generous next to a
# chat turn's memory, inside the client's 48 KB initial-context frame.
SNAPSHOT_BUDGET = 30_000
# The speaking model's profile: a short orientation, not the memory.
PROFILE_BUDGET = 2_000
RECENT_DAYS = 3
KG_ENTITIES = 20
TRUNCATION_NOTE = "[More is in the agent's memory; search it with flowly_recall.]"


@dataclass(frozen=True)
class Section:
    kind: str
    title: str
    text: str
    source: str

    def wire(self) -> dict[str, str]:
        return {'kind': self.kind, 'title': self.title, 'text': self.text, 'sourceRef': self.source}


def validate_snapshot(params: dict) -> dict:
    if set(params) - {'knownRevision'}:
        raise VoiceError('INVALID_PARAMS', 'The snapshot accepts only knownRevision; the runtime owns its scope.')
    known = params.get('knownRevision')
    if known is not None and (not isinstance(known, str) or not re.fullmatch(r'[0-9a-f]{24}', known)):
        raise VoiceError('INVALID_PARAMS', 'knownRevision must be a snapshot revision.')
    return {'knownRevision': known}


def _paragraphs(text: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r'\n\s*\n', text) if chunk.strip()]


class VoiceMemorySnapshot:
    def __init__(self, workspace: Path, *, state_db: Callable[[str], Path],
                 profile: Callable[[], tuple[str, str]], persona: Callable[[], str] = lambda: 'default',
                 search_enabled: Callable[[], bool] = lambda: False,
                 today: Callable[[], datetime] = lambda: datetime.now()):
        self.workspace = workspace.resolve()
        self.state_db = state_db
        self.profile = profile
        self.persona = persona
        self.search_enabled = search_enabled
        self.today = today

    def snapshot(self, params: dict) -> dict:
        request = validate_snapshot(params)
        scope_profile, bot_id = self.profile()
        scope = {'profile': scope_profile, 'botId': bot_id}
        governed = GovernedMemory.load(self.state_db, datetime.now(timezone.utc).isoformat())
        if not governed.available:
            return {'scope': scope, 'contentRole': 'reference_data', 'revision': revision_of(['unavailable']),
                    'sections': [], 'profile': '', 'truncated': False, 'partial': True,
                    'sources': {'governance': {'status': 'unavailable'}}}

        sources: dict[str, dict] = {'governance': {'status': governed.status, 'revision': governed.revision}}
        sections: list[Section] = []

        def file_section(kind: str, title: str, relative: str, key: str | None = None) -> None:
            key = key or kind
            try:
                source = read_memory_file(self.workspace, relative)
            except (OSError, ValueError, UnicodeDecodeError):
                sources[key] = {'status': 'unavailable'}
                return
            if source is None:
                sources[key] = {'status': 'empty'}
                return
            raw, revision, _ = source
            text = self._safe(raw if kind != 'notes' else manual_memory(raw), relative, governed)
            sources[key] = {'status': 'ok' if text else 'empty', 'revision': revision}
            if text:
                sections.append(Section(kind, title, text, f'memory://file/{relative}'))

        # The agent's identity, in the order its own prompt loads it.
        file_section('soul', 'Soul', 'SOUL.md')
        persona = self.persona()
        if persona and persona != 'default' and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', persona):
            file_section('persona', f'Active persona: {persona}', f'personas/{persona}.md')
        file_section('identity', 'Identity', 'IDENTITY.md')
        file_section('user', 'User', 'USER.md')

        # Governed memory, rendered as MEMORY.md renders it, from the items a
        # call may see (fresher than the file: no wait for the next render).
        memory = self._governed_block(governed, sources)
        if memory:
            sections.append(Section('memory', 'Memory', memory, 'memory://governance'))
        file_section('notes', 'Notes', 'memory/MEMORY.md')

        # Without memory search the agent's prompt carries its recent notes.
        if not self.search_enabled():
            for offset in range(RECENT_DAYS):
                day = (self.today() - timedelta(days=offset)).strftime('%Y-%m-%d')
                file_section('recent', f'Notes from {day}', f'memory/{day}.md', key=f'recent:{day}')

        budgeted, truncated = self._budget(sections)
        revision = revision_of([section.wire() for section in budgeted])
        if request['knownRevision'] == revision:
            return {'scope': scope, 'contentRole': 'reference_data', 'revision': revision, 'unchanged': True}
        return {'scope': scope, 'contentRole': 'reference_data', 'revision': revision,
                'sections': [section.wire() for section in budgeted],
                'profile': self._profile(budgeted), 'truncated': truncated,
                'partial': any(source.get('status') == 'unavailable' for source in sources.values()),
                'sources': sources}

    def _safe(self, text: str, name: str, governed: GovernedMemory) -> str:
        """The paragraphs a call may see, through the agent's injection scan."""
        from flowly.cron.guard import scan_context_file

        kept = [chunk for chunk in _paragraphs(text) if not governed.withholds(chunk)]
        joined = redact_secrets('\n\n'.join(kept)).strip()
        if not joined:
            return ''
        blocked = scan_context_file(joined, name)
        return blocked or joined

    def _governed_block(self, governed: GovernedMemory, sources: dict) -> str:
        from flowly.memory.governance import GovernanceStore
        from flowly.memory.knowledge_graph import KnowledgeGraph
        from flowly.memory.summary import SENTINEL_END, SENTINEL_START, render_generated_block

        try:
            items = [GovernanceStore._row_to_item(row) for row in governed.allowed]
        except (KeyError, TypeError, ValueError):
            sources['governance'] = {'status': 'unavailable'}
            return ''
        kg_summary = ''
        path = self.state_db('knowledge_graph.sqlite3')
        try:
            if path.exists():
                kg_summary = KnowledgeGraph(str(path)).summary(
                    max_entities=KG_ENTITIES, exclude_triple_ids=governed.excluded_triples)
            sources['knowledge'] = {'status': 'ok' if kg_summary else 'empty', 'revision': revision_of(kg_summary)}
        except Exception:
            sources['knowledge'] = {'status': 'unavailable'}
        if not items and not kg_summary:
            return ''
        block = render_generated_block(items, kg_summary)
        # The markers and the edit warning are for people editing the file.
        lines = [line for line in block.splitlines()
                 if line not in (SENTINEL_START, SENTINEL_END) and not line.startswith('<!--')]
        text = '\n'.join(lines).strip()
        if governed.withholds(text):
            text = '\n\n'.join(chunk for chunk in _paragraphs(text) if not governed.withholds(chunk))
        return redact_secrets(text).strip()

    @staticmethod
    def _budget(sections: list[Section]) -> tuple[list[Section], bool]:
        """Keep sections in priority order within ``SNAPSHOT_BUDGET``, cutting
        only at a paragraph and saying so."""
        kept: list[Section] = []
        left = SNAPSHOT_BUDGET
        truncated = False
        for section in sections:
            if left <= len(TRUNCATION_NOTE) + 2:
                truncated = True
                break
            if len(section.text) <= left:
                kept.append(section)
                left -= len(section.text)
                continue
            truncated = True
            room = left - len(TRUNCATION_NOTE) - 2
            taken: list[str] = []
            for chunk in _paragraphs(section.text):
                if len(chunk) + 2 > room:
                    break
                taken.append(chunk)
                room -= len(chunk) + 2
            if taken:
                text = '\n\n'.join(taken + [TRUNCATION_NOTE])
                kept.append(Section(section.kind, section.title, text, section.source))
                left -= len(text)
            break
        return kept, truncated

    @staticmethod
    def _profile(sections: list[Section]) -> str:
        """Who the agent is and who the user is, for the speaking model."""
        by_kind = {section.kind: section.text for section in sections}
        parts: list[str] = []
        for kind, label, share in (('identity', 'Agent', 380), ('soul', 'Character', 380),
                                   ('user', 'User', 560), ('memory', 'Known about the user', 560)):
            text = by_kind.get(kind, '')
            if not text:
                continue
            excerpt = text if len(text) <= share else text[:share].rsplit('\n', 1)[0].rstrip() + ' …'
            parts.append(f'{label}:\n{excerpt}')
        profile = '\n\n'.join(parts)
        return profile if len(profile) <= PROFILE_BUDGET else profile[:PROFILE_BUDGET].rsplit('\n', 1)[0]
