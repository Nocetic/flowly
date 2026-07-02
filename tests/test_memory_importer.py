from __future__ import annotations

import pytest

from flowly.memory.dreamer import Candidate
from flowly.memory.governance import (
    STATUS_ACTIVE,
    STATUS_NEEDS_REVIEW,
    GovernanceStore,
)
from flowly.memory.importer import (
    IMPORT_META_PREFIX,
    MemoryDumpExtractor,
    chunk_dump,
    memory_export_prompt,
    parse_dump,
    run_import,
)


@pytest.fixture()
def gov(tmp_path):
    store = GovernanceStore(tmp_path / "gov.sqlite3")
    yield store
    store.close()


class FakeExtractor:
    def __init__(self, batches):
        self.batches = batches
        self.calls = 0
        self.kwargs = []

    def extract(self, dump, **kwargs):
        self.kwargs.append(kwargs)
        batch = self.batches[self.calls] if self.calls < len(self.batches) else []
        self.calls += 1
        return batch


def test_parse_and_chunk_dump():
    parsed = parse_dump("# Identity\n- Name is Hakan\n- Uses Flowly\n")
    assert parsed.rough_item_count == 2
    assert parsed.line_count == 3

    chunks = chunk_dump("first paragraph\n\n" + ("x" * 50), max_chars=20)
    assert len(chunks) > 1
    assert all(len(c) <= 20 for c in chunks)


def test_import_puts_candidates_in_review_with_metadata(gov):
    calls = []
    cand = Candidate(
        kind="preference",
        text="Prefers pytest",
        normalized_key="pref:test",
        confidence=0.9,
        is_explicit=True,
    )
    res = run_import(
        gov,
        provider=object(),
        model="m",
        text="- Prefers pytest",
        source="chatgpt",
        extractor=FakeExtractor([[cand]]),
        on_committed=lambda: calls.append(1),
    )

    assert res.ran is True
    assert res.activated == 0
    assert res.needs_review == 1
    assert calls == [1]

    items = gov.list_items(status=STATUS_NEEDS_REVIEW)
    assert len(items) == 1
    item = items[0]
    assert item.text == "Prefers pytest"
    assert item.source_session.startswith("import:chatgpt:")
    assert item.metadata["source"] == "chatgpt"
    assert item.metadata["source_label"] == "ChatGPT"
    assert item.metadata["dump_sha256"] == res.dump_hash
    assert gov.get_meta(f"{IMPORT_META_PREFIX}chatgpt:{res.dump_hash}")

    again = run_import(
        gov,
        provider=object(),
        model="m",
        text="- Prefers pytest",
        source="chatgpt",
        extractor=FakeExtractor([[cand]]),
    )
    assert again.ran is False
    assert again.reason == "already_imported"
    assert len(gov.list_items()) == 1


def test_import_conflict_never_supersedes_active_fact(gov):
    old = gov.add_item(
        kind="profile",
        text="Lives in Istanbul",
        normalized_key="profile:location",
        confidence=0.9,
    )
    gov.transition(old.id, STATUS_ACTIVE, reason="seed")

    new = Candidate(
        kind="profile",
        text="Lives in Ankara",
        normalized_key="profile:location",
        confidence=1.0,
        is_explicit=True,
    )
    res = run_import(
        gov,
        provider=object(),
        model="m",
        text="- Lives in Ankara",
        source="gemini",
        extractor=FakeExtractor([[new]]),
        force=True,
    )

    assert res.conflicts == 1
    assert res.needs_review == 1
    active = gov.list_items(status=STATUS_ACTIVE)
    assert [i.text for i in active] == ["Lives in Istanbul"]
    review = gov.list_items(status=STATUS_NEEDS_REVIEW)
    assert [i.text for i in review] == ["Lives in Ankara"]


class _Delta:
    def __init__(self, content):
        self.content = content


class _Provider:
    async def chat_stream(self, messages, **kwargs):
        yield _Delta(
            '[{"kind":"project","text":"Building Flowly","normalized_key":"project:flowly",'
            '"privacy_level":"normal","confidence":0.8}]'
        )


def test_memory_dump_extractor_parses_and_stamps_candidates():
    ex = MemoryDumpExtractor(provider=_Provider(), model="m")
    cands = ex.extract(
        "- Building Flowly",
        source="gemini",
        run_id="abc123",
        dump_hash="h" * 64,
        chunk_index=0,
        total_chunks=1,
    )

    assert len(cands) == 1
    assert cands[0].kind == "project"
    assert cands[0].source_session == "import:gemini:abc123"
    assert cands[0].source_message_ids == ["chunk:1"]
    assert cands[0].is_explicit is False
    assert cands[0].metadata["source"] == "gemini"


def test_export_prompt_sources():
    assert "ChatGPT" in memory_export_prompt("chatgpt")
    assert "Google Gemini" in memory_export_prompt("gemini")
