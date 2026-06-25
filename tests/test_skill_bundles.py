"""Tests for the skill bundles loader / resolver.

Covers slugify normalisation, YAML parsing edge cases, the mtime
cache, and the slash-prefix expansion path that the agent loop applies
before normal LLM turns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from flowly.agent import skill_bundles


# --------------------------------------------------------------------- #
# Test fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``get_flowly_home()`` at a clean per-test directory.

    Also resets the bundle cache so each test starts from a known state.
    """
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    skill_bundles.reload()
    yield tmp_path
    skill_bundles.reload()


def _write_bundle(home: Path, filename: str, content: dict) -> Path:
    bundles_dir = home / "skill-bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    path = bundles_dir / filename
    path.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")
    return path


def _write_skill(home: Path, name: str, body: str = "skill body") -> Path:
    """Stash a SKILL.md in the managed skills dir so the resolver finds it."""
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------- #
# Slugify
# --------------------------------------------------------------------- #


def test_slugify_lowercase_and_hyphen():
    assert skill_bundles._slugify("Research Tools") == "research-tools"
    assert skill_bundles._slugify("backend_dev") == "backend-dev"
    assert skill_bundles._slugify("ALREADY-HYPHEN") == "already-hyphen"


def test_slugify_collapses_repeats():
    assert skill_bundles._slugify("  weird---name__here  ") == "weird-name-here"


def test_slugify_strips_invalid_chars():
    assert skill_bundles._slugify("foo!@#$bar") == "foo-bar"


def test_slugify_empty_input():
    assert skill_bundles._slugify("") == ""
    assert skill_bundles._slugify("   ") == ""


def test_canonical_key_handles_slash_prefix():
    assert skill_bundles._canonical_key("research") == "/research"
    assert skill_bundles._canonical_key("/research") == "/research"
    assert skill_bundles._canonical_key("/Research Tools") == "/research-tools"
    assert skill_bundles._canonical_key("") == ""


# --------------------------------------------------------------------- #
# Scan + cache
# --------------------------------------------------------------------- #


def test_scan_empty_dir(isolated_home: Path):
    assert skill_bundles.scan_bundles() == {}


def test_scan_single_bundle(isolated_home: Path):
    _write_bundle(isolated_home, "research.yaml", {
        "name": "Research",
        "description": "Web research workflow",
        "skills": ["web-search", "arxiv"],
    })
    bundles = skill_bundles.scan_bundles()
    assert "/research" in bundles
    bundle = bundles["/research"]
    assert bundle["name"] == "Research"
    assert bundle["skills"] == ["web-search", "arxiv"]
    assert bundle["description"] == "Web research workflow"


def test_scan_skips_invalid_yaml(isolated_home: Path, caplog):
    bundles_dir = isolated_home / "skill-bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    (bundles_dir / "broken.yaml").write_text("not: valid: yaml: [unterminated\n")
    # Still scans, just skips the broken one.
    assert skill_bundles.scan_bundles() == {}


def test_scan_skips_missing_skills_key(isolated_home: Path):
    _write_bundle(isolated_home, "noskills.yaml", {"name": "Empty"})
    assert skill_bundles.scan_bundles() == {}


def test_scan_skips_non_string_skill_entries(isolated_home: Path):
    _write_bundle(isolated_home, "mixed.yaml", {
        "name": "Mixed",
        "skills": ["good-skill", 42, None, "another-good"],
    })
    bundle = skill_bundles.scan_bundles()["/mixed"]
    assert bundle["skills"] == ["good-skill", "another-good"]


def test_scan_dedupes_by_slug(isolated_home: Path):
    """Two files producing the same slug: first wins, second is logged."""
    _write_bundle(isolated_home, "a-bundle.yaml", {
        "name": "A Bundle",
        "skills": ["one"],
    })
    _write_bundle(isolated_home, "z-bundle.yaml", {
        "name": "A Bundle",  # same slug, different filename
        "skills": ["two"],
    })
    bundles = skill_bundles.scan_bundles()
    assert len(bundles) == 1
    assert bundles["/a-bundle"]["skills"] == ["one"]  # alphabetical filename wins


def test_cache_refreshes_when_file_changes(isolated_home: Path):
    path = _write_bundle(isolated_home, "x.yaml", {
        "name": "X",
        "skills": ["one"],
    })
    bundles_before = skill_bundles.scan_bundles()
    assert bundles_before["/x"]["skills"] == ["one"]

    # Touch the file with a newer mtime + different content.
    path.write_text(yaml.safe_dump({
        "name": "X",
        "skills": ["one", "two"],
    }, sort_keys=False), encoding="utf-8")
    os.utime(path, (path.stat().st_atime + 10, path.stat().st_mtime + 10))

    bundles_after = skill_bundles.scan_bundles()
    assert bundles_after["/x"]["skills"] == ["one", "two"]


def test_reload_clears_cache(isolated_home: Path):
    _write_bundle(isolated_home, "x.yaml", {"name": "X", "skills": ["one"]})
    assert "/x" in skill_bundles.scan_bundles()
    skill_bundles.reload()
    # New scan finds it again (file still on disk), but the cache was rebuilt.
    assert "/x" in skill_bundles.scan_bundles()


# --------------------------------------------------------------------- #
# is_bundle_command + maybe_expand
# --------------------------------------------------------------------- #


def test_is_bundle_command_recognises_known(isolated_home: Path):
    _write_bundle(isolated_home, "research.yaml", {
        "name": "Research",
        "skills": ["web-search"],
    })
    _write_skill(isolated_home, "web-search")
    assert skill_bundles.is_bundle_command("/research solar trends")
    assert skill_bundles.is_bundle_command("/Research")  # case-insensitive
    assert not skill_bundles.is_bundle_command("/unknown")
    assert not skill_bundles.is_bundle_command("plain message")


def test_maybe_expand_noop_when_no_match(isolated_home: Path):
    """Non-bundle/non-skill messages flow through unchanged."""
    assert skill_bundles.maybe_expand("hello world") == "hello world"
    assert skill_bundles.maybe_expand("/unknown thing") == "/unknown thing"
    assert skill_bundles.maybe_expand("") == ""


def test_maybe_expand_injects_skill_body(isolated_home: Path):
    _write_skill(isolated_home, "web-search", "Use a search engine.")
    _write_skill(isolated_home, "arxiv", "Search arXiv preprints.")
    _write_bundle(isolated_home, "research.yaml", {
        "name": "Research",
        "description": "Web research workflow",
        "skills": ["web-search", "arxiv"],
        "instruction": "Cite sources.",
    })

    expanded = skill_bundles.maybe_expand("/research solar panels")
    assert "[BUNDLE] Research" in expanded
    assert "Loaded skills: web-search, arxiv" in expanded
    assert "Use a search engine." in expanded
    assert "Search arXiv preprints." in expanded
    assert "[Bundle instruction] Cite sources." in expanded
    assert "[Task] solar panels" in expanded


def test_maybe_expand_reports_missing_skills(isolated_home: Path):
    """Bundle referencing a non-existent skill loads partial."""
    _write_skill(isolated_home, "real-skill", "Real skill body.")
    _write_bundle(isolated_home, "half.yaml", {
        "name": "Half",
        "skills": ["real-skill", "ghost-skill"],
    })
    expanded = skill_bundles.maybe_expand("/half do thing")
    assert "Real skill body." in expanded
    assert "Skills not loaded (missing or unavailable): ghost-skill" in expanded


def test_maybe_expand_dedupes_repeated_skill_entries(isolated_home: Path):
    _write_skill(isolated_home, "one", "ONE body")
    _write_bundle(isolated_home, "dupe.yaml", {
        "name": "Dupe",
        "skills": ["one", "one", "one"],
    })
    expanded = skill_bundles.maybe_expand("/dupe")
    # Header only mentions one copy; body only appears once.
    assert expanded.count("ONE body") == 1


def test_maybe_expand_no_user_instruction(isolated_home: Path):
    _write_skill(isolated_home, "one", "body")
    _write_bundle(isolated_home, "x.yaml", {"name": "X", "skills": ["one"]})
    expanded = skill_bundles.maybe_expand("/x")
    assert "[Task]" not in expanded


def test_scan_skill_commands_surfaces_managed_skill(isolated_home: Path):
    _write_skill(isolated_home, "writer", "Write clearly.")
    commands = skill_bundles.scan_skill_commands()
    assert commands["/writer"]["name"] == "writer"
    assert commands["/writer"]["source"] == "managed"
    assert commands["/writer"]["path"].endswith("writer/SKILL.md")


def test_maybe_expand_injects_single_skill_body(isolated_home: Path):
    skill_path = _write_skill(isolated_home, "writer", "Write clearly.")
    expanded = skill_bundles.maybe_expand("/writer draft intro")
    assert 'The user has invoked the "writer" skill' in expanded
    assert "Write clearly." in expanded
    assert f"[Skill directory: {skill_path.parent}]" in expanded
    assert "[Task] draft intro" in expanded


def test_maybe_expand_single_skill_lists_supporting_files(isolated_home: Path):
    skill_path = _write_skill(isolated_home, "writer", "Write clearly.")
    ref = skill_path.parent / "references" / "style.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("Use short sentences.", encoding="utf-8")
    expanded = skill_bundles.maybe_expand("/writer")
    assert "[This skill has supporting files:]" in expanded
    assert "references/style.md" in expanded
    assert 'skill_view(name="writer", file_path="<path>")' in expanded


def test_maybe_expand_does_not_shadow_builtin_command(isolated_home: Path):
    _write_skill(isolated_home, "status", "Do not load me.")
    assert skill_bundles.maybe_expand("/status") == "/status"


def test_maybe_expand_bundle_wins_over_same_slug_skill(isolated_home: Path):
    _write_skill(isolated_home, "research", "Single skill body.")
    _write_skill(isolated_home, "web-search", "Bundle skill body.")
    _write_bundle(isolated_home, "research.yaml", {
        "name": "Research",
        "skills": ["web-search"],
    })
    expanded = skill_bundles.maybe_expand("/research task")
    assert "[BUNDLE] Research" in expanded
    assert "Bundle skill body." in expanded
    assert "Single skill body." not in expanded


# --------------------------------------------------------------------- #
# save_bundle + delete_bundle
# --------------------------------------------------------------------- #


def test_save_bundle_creates_yaml(isolated_home: Path):
    path = skill_bundles.save_bundle(
        name="My Bundle",
        skills=["a", "b"],
        description="desc",
        instruction="inst",
    )
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data["name"] == "My Bundle"
    assert data["skills"] == ["a", "b"]
    assert data["description"] == "desc"
    assert data["instruction"] == "inst"


def test_save_bundle_rejects_existing_without_overwrite(isolated_home: Path):
    skill_bundles.save_bundle(name="X", skills=["one"])
    with pytest.raises(FileExistsError):
        skill_bundles.save_bundle(name="X", skills=["two"])


def test_save_bundle_overwrite_replaces(isolated_home: Path):
    skill_bundles.save_bundle(name="X", skills=["one"])
    skill_bundles.save_bundle(name="X", skills=["two"], overwrite=True)
    bundles = skill_bundles.scan_bundles()
    assert bundles["/x"]["skills"] == ["two"]


def test_save_bundle_empty_skills_raises(isolated_home: Path):
    with pytest.raises(ValueError):
        skill_bundles.save_bundle(name="X", skills=[])


def test_delete_bundle_removes_file(isolated_home: Path):
    skill_bundles.save_bundle(name="X", skills=["one"])
    assert "/x" in skill_bundles.scan_bundles()

    deleted = skill_bundles.delete_bundle("/x")
    assert deleted is not None
    assert not deleted.exists()
    assert "/x" not in skill_bundles.scan_bundles()


def test_delete_bundle_unknown_returns_none(isolated_home: Path):
    assert skill_bundles.delete_bundle("/never-existed") is None
