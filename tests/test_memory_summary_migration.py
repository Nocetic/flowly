"""P1 tests: generated MEMORY.md block (manual-content preservation) + migration."""

from __future__ import annotations

import pytest

from flowly.agent.memory import MemoryStore
from flowly.memory.governance import (
    GovernanceStore,
    STATUS_ACTIVE,
)
from flowly.memory.migration import (
    kg_value_tokens,
    migrate_memory_md,
    parse_freeform_entries,
)
from flowly.memory.summary import (
    SENTINEL_END,
    SENTINEL_START,
    extract_manual_content,
    regenerate_memory_md,
    render_generated_block,
    splice_generated_block,
)


@pytest.fixture
def gov(tmp_path):
    s = GovernanceStore(tmp_path / "gov.sqlite3")
    yield s
    s.close()


# -- render -----------------------------------------------------------------


def test_render_only_active_items(gov):
    a = gov.add_item(kind="preference", text="dark mode")
    gov.add_item(kind="preference", text="candidate only")  # stays candidate
    gov.transition(a.id, STATUS_ACTIVE)
    block = render_generated_block(gov.list_items())
    assert SENTINEL_START in block and SENTINEL_END in block
    assert "dark mode" in block
    assert "candidate only" not in block


def test_render_groups_and_kg(gov):
    p = gov.add_item(kind="profile", text="name is Hakan")
    gov.transition(p.id, STATUS_ACTIVE)
    block = render_generated_block(gov.list_items(), kg_summary="- Hakan (person): email=a@b.com")
    assert "## Profile" in block
    assert "## Knowledge Graph" in block
    assert "email=a@b.com" in block


def test_render_empty(gov):
    block = render_generated_block(gov.list_items())
    assert "_No active memory yet._" in block


# -- splice preserves manual content ----------------------------------------


def test_splice_into_empty():
    block = render_generated_block([])
    out = splice_generated_block("", block)
    assert block in out


def test_splice_preserves_surrounding_manual():
    manual_before = "# My hand-written notes\n\nremember the milk\n"
    manual_after = "\n## footer I wrote\n\nkeep me too\n"
    existing = manual_before + "\n" + SENTINEL_START + "\nold gen\n" + SENTINEL_END + manual_after
    new_block = SENTINEL_START + "\nNEW GENERATED\n" + SENTINEL_END
    out = splice_generated_block(existing, new_block)
    assert "remember the milk" in out
    assert "keep me too" in out
    assert "NEW GENERATED" in out
    assert "old gen" not in out  # replaced, not duplicated
    assert out.count(SENTINEL_START) == 1
    assert out.count(SENTINEL_END) == 1


def test_splice_appends_when_no_region():
    existing = "just manual notes, no markers\n"
    block = render_generated_block([])
    out = splice_generated_block(existing, block)
    assert "just manual notes" in out
    assert SENTINEL_START in out


def test_splice_is_stable_across_regen():
    manual = "manual top\n"
    existing = manual + "\n" + render_generated_block([])
    out1 = splice_generated_block(existing, render_generated_block([]))
    out2 = splice_generated_block(out1, render_generated_block([]))
    assert out1 == out2  # idempotent
    assert out2.count(SENTINEL_START) == 1


def test_extract_manual_content():
    existing = "before\n\n" + SENTINEL_START + "\ngen\n" + SENTINEL_END + "\n\nafter"
    manual = extract_manual_content(existing)
    assert "before" in manual and "after" in manual
    assert "gen" not in manual


# -- parse legacy entries ---------------------------------------------------


def test_parse_timestamped_entries():
    md = (
        "<!-- 2026-06-01 10:00 -->\nlikes dark mode\n"
        "<!-- 2026-06-02 11:00 -->\nuses zsh on macOS\n"
    )
    entries = parse_freeform_entries(md)
    assert entries == ["likes dark mode", "uses zsh on macOS"]


def test_parse_paragraph_fallback_and_skips_headers():
    md = "# 2026-06-05\n\nfirst note\n\nsecond note"
    entries = parse_freeform_entries(md)
    assert entries == ["first note", "second note"]


def test_parse_empty():
    assert parse_freeform_entries("   ") == []


# -- migration --------------------------------------------------------------


def test_migrate_imports_candidates(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    ms.write_long_term("<!-- 2026-06-01 10:00 -->\nlikes dark mode\n<!-- 2026-06-02 -->\nuses zsh\n")
    res = migrate_memory_md(gov, ms)
    assert res.migrated is True
    assert res.imported == 2
    assert res.backup_path is not None
    items = gov.list_items(ref_kind="memory_md")
    assert {i.text for i in items} == {"likes dark mode", "uses zsh"}


def test_migrate_backs_up_original(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    ms.write_long_term("<!-- 2026-06-01 -->\nsomething\n")
    res = migrate_memory_md(gov, ms)
    from pathlib import Path
    assert Path(res.backup_path).read_text() == "<!-- 2026-06-01 -->\nsomething\n"


def test_migrate_is_idempotent(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    ms.write_long_term("<!-- 2026-06-01 -->\nfact one\n")
    first = migrate_memory_md(gov, ms)
    second = migrate_memory_md(gov, ms)
    assert first.migrated is True
    assert second.migrated is False
    assert second.reason == "already_migrated"
    assert len(gov.list_items(ref_kind="memory_md")) == 1  # no double-import


def test_migrate_dedups_internal_duplicates(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    ms.write_long_term("<!-- a -->\nsame note\n<!-- b -->\nsame note\n")
    # marker 'a'/'b' lack full date; ensure regex still splits on date-led ones
    ms.write_long_term("<!-- 2026-06-01 -->\nsame note\n<!-- 2026-06-02 -->\nsame note\n")
    res = migrate_memory_md(gov, ms)
    assert res.imported == 1
    assert res.duplicates == 1


def test_migrate_skips_kg_covered_entries(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    ms.write_long_term(
        "<!-- 2026-06-01 -->\nmy email is hakan@nocetic.com\n"
        "<!-- 2026-06-02 -->\nprefers tabs over spaces\n"
    )
    # Simulate the email already living in the KG.
    tokens = {"hakan@nocetic.com"}
    res = migrate_memory_md(gov, ms, kg_tokens=tokens)
    assert res.imported == 1
    assert res.kg_skipped == 1
    texts = {i.text for i in gov.list_items(ref_kind="memory_md")}
    assert texts == {"prefers tabs over spaces"}


def test_migrate_empty_memory(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    res = migrate_memory_md(gov, ms)
    assert res.migrated is False
    assert res.reason == "empty"


def test_regenerate_memory_md_preserves_manual(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    ms.write_long_term("# my notes\n\nremember milk\n")
    a = gov.add_item(kind="preference", text="dark mode")
    gov.transition(a.id, STATUS_ACTIVE)
    out = regenerate_memory_md(gov, ms, kg_summary="- Hakan (person): email=a@b.com")
    assert "remember milk" in out          # manual preserved
    assert "dark mode" in out              # active item rendered
    assert "email=a@b.com" in out          # KG summary included
    assert ms.read_long_term() == out      # written to disk
    # idempotent regen keeps a single block
    out2 = regenerate_memory_md(gov, ms, kg_summary="- Hakan (person): email=a@b.com")
    assert out2.count(SENTINEL_START) == 1


def test_regenerate_omits_secret_items(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    pub = gov.add_item(kind="preference", text="likes tea")
    sec = gov.add_item(kind="profile", text="ssn 123-45-6789", privacy_level="secret")
    gov.transition(pub.id, STATUS_ACTIVE)
    gov.transition(sec.id, STATUS_ACTIVE)  # even if forced active
    out = regenerate_memory_md(gov, ms)
    assert "likes tea" in out
    assert "123-45-6789" not in out  # secret never written to MEMORY.md


def test_dreamer_to_memory_md_end_to_end(gov, tmp_path):
    """Active item produced by a dreamer pass becomes visible in MEMORY.md."""
    from flowly.memory.dreamer import Candidate, MemoryDreamerService, MessageRow

    ms = MemoryStore(tmp_path / "ws")

    class _Delta:
        def read_since(self, wm, limit):
            return [MessageRow(id=1, session_key="s", role="user", content="x", timestamp=1.0)]

    class _Extract:
        def extract(self, delta, known=(), profile=""):
            return [Candidate(kind="preference", text="prefers vim",
                              normalized_key="editor", confidence=0.9)]

    svc = MemoryDreamerService(
        gov, _Delta(), _Extract(),
        injection_check=lambda t: False,
        on_committed=lambda: regenerate_memory_md(gov, ms),
    )
    svc.run()
    assert "prefers vim" in ms.read_long_term()


def test_kg_value_tokens_reads_real_kg(tmp_path):
    from flowly.memory.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(str(tmp_path / "kg.sqlite3"))
    kg.add_triple("Hakan Ören", "email", "hakan@nocetic.com", subject_type="person")
    kg.add_triple("Hakan Ören", "works_at", "Nocetic Limited",
                  subject_type="person", object_type="company")
    tokens = kg_value_tokens(kg)
    assert "hakan@nocetic.com" in tokens
    # entity object normalized (underscores → spaces)
    assert any("nocetic" in t for t in tokens)
