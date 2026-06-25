"""Tests for the SkillsLoader two-tier cache (LRU + disk snapshot).

The snapshot layer at ``~/.flowly/.skills_prompt_snapshot.json`` is
a cold-path optimisation: it speeds up the first ``build_skills_summary``
call after a Flowly restart from a full filesystem walk + frontmatter
parse to a single JSON read + manifest stat.

What we pin:

  * **Cold path** writes a snapshot file with the right schema version.
  * **Warm path** (LRU empty, snapshot present) returns the same XML
    without re-reading any SKILL.md.
  * **Manifest invalidation** — touching / editing a SKILL.md flips
    the snapshot to invalid; next build returns updated content.
  * **Version bump** invalidates every snapshot regardless of manifest.
  * **Corrupt snapshot** degrades to the cold path silently.
  * **Cache key sensitivity** — same on-disk skill set with different
    ``available_tools`` produces the right filtered XML (snapshot is
    metadata-only, filter happens at LRU layer).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from textwrap import dedent

import pytest

from flowly.agent.skills import (
    _SKILLS_PROMPT_CACHE,
    _SKILLS_SNAPSHOT_VERSION,
    SkillsLoader,
    _skills_snapshot_path,
    clear_skills_snapshot,
)


@pytest.fixture
def isolated_flowly_home(tmp_path, monkeypatch):
    """Redirect FLOWLY_HOME to a per-test directory so the snapshot
    file lives there instead of polluting ~/.flowly.
    """
    home = tmp_path / "flowly_home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    # The skills module reads FLOWLY_HOME lazily via get_flowly_home(),
    # so the env var is enough — no module-level re-import needed.
    yield home
    clear_skills_snapshot()


@pytest.fixture
def workspace_with_one_skill(tmp_path):
    """Build a minimal workspace/skills/<name>/SKILL.md tree."""
    ws = tmp_path / "ws"
    skill_dir = ws / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(dedent("""\
        ---
        name: test-skill
        description: A minimal skill for snapshot tests
        ---
        # Test Skill

        Body content.
    """), encoding="utf-8")
    return ws, skill_file


def _fresh_loader(workspace: Path) -> SkillsLoader:
    """Build a SkillsLoader with NO builtin skill dir.

    The package's bundled SKILL.md files would otherwise leak into
    every test and force us to assert on a moving target. Passing a
    non-existent path scopes the loader to the test workspace alone.
    """
    _SKILLS_PROMPT_CACHE.clear()
    return SkillsLoader(
        workspace=workspace,
        builtin_skills_dir=workspace / "__no_builtin__",
    )


# ---------------------------------------------------------------------------
# Cold path → writes snapshot
# ---------------------------------------------------------------------------


class TestColdPath:
    def test_first_call_writes_snapshot(
        self, isolated_flowly_home, workspace_with_one_skill,
    ) -> None:
        ws, _ = workspace_with_one_skill
        loader = _fresh_loader(ws)

        snap_path = _skills_snapshot_path()
        assert not snap_path.exists()

        xml = loader.build_skills_summary()
        assert "test-skill" in xml
        assert "A minimal skill" in xml
        assert snap_path.exists(), "cold path must write the snapshot"

        # Snapshot is JSON with the right schema.
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        assert payload["version"] == _SKILLS_SNAPSHOT_VERSION
        assert isinstance(payload["manifest"], dict)
        assert isinstance(payload["entries"], list)
        assert len(payload["entries"]) == 1
        entry = payload["entries"][0]
        assert entry["name"] == "test-skill"
        assert entry["source"] == "workspace"
        # Description carries through (truncation only kicks in past 60 chars).
        assert "minimal skill" in entry["description"].lower()

    def test_cold_path_with_no_skills_writes_nothing(
        self, isolated_flowly_home, tmp_path,
    ) -> None:
        # An empty workspace with no skills returns "" and must NOT
        # leave a snapshot behind (a snapshot with empty `entries`
        # would just waste an inode and confuse later debugging).
        ws = tmp_path / "empty_ws"
        ws.mkdir()
        loader = _fresh_loader(ws)
        assert loader.build_skills_summary() == ""
        assert not _skills_snapshot_path().exists()


# ---------------------------------------------------------------------------
# Warm path → disk hit, no filesystem walk
# ---------------------------------------------------------------------------


class TestWarmPath:
    def test_second_call_after_lru_clear_uses_disk_snapshot(
        self, isolated_flowly_home, workspace_with_one_skill,
    ) -> None:
        ws, _ = workspace_with_one_skill
        loader = _fresh_loader(ws)

        xml1 = loader.build_skills_summary()
        _SKILLS_PROMPT_CACHE.clear()  # simulate a fresh process

        # The cold path wrote a snapshot; the next build should serve
        # from disk without invoking the (slow) frontmatter parser.
        # We can't directly intercept the parser, but a much-faster
        # render path is the best proxy: the cold path took ~10-30ms
        # in practice, the warm path runs under 5ms.
        t0 = time.perf_counter()
        xml2 = loader.build_skills_summary()
        warm_ms = (time.perf_counter() - t0) * 1000

        assert xml1 == xml2
        assert warm_ms < 50, (
            f"warm path took {warm_ms:.1f}ms — snapshot may not have been used"
        )

    def test_lru_hit_does_not_touch_disk(
        self, isolated_flowly_home, workspace_with_one_skill,
    ) -> None:
        ws, _ = workspace_with_one_skill
        loader = _fresh_loader(ws)
        xml1 = loader.build_skills_summary()

        # Delete the snapshot file. If a subsequent call honoured the
        # LRU, it must NOT need to re-read the (now missing) snapshot.
        _skills_snapshot_path().unlink()
        xml2 = loader.build_skills_summary()
        assert xml1 == xml2
        # And the LRU hit must not have re-written the snapshot.
        assert not _skills_snapshot_path().exists()


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestInvalidation:
    def test_skill_edit_invalidates_snapshot(
        self, isolated_flowly_home, workspace_with_one_skill,
    ) -> None:
        ws, skill_file = workspace_with_one_skill
        loader = _fresh_loader(ws)
        xml1 = loader.build_skills_summary()
        assert "A minimal skill" in xml1

        # Edit the SKILL.md; manifest mtime changes; snapshot becomes
        # stale; the warm path must rebuild and reflect the new content.
        time.sleep(0.02)  # ensure mtime tick
        skill_file.write_text(dedent("""\
            ---
            name: test-skill
            description: Now totally different content
            ---
            # Test
            New body.
        """), encoding="utf-8")
        _SKILLS_PROMPT_CACHE.clear()

        xml2 = loader.build_skills_summary()
        assert "Now totally different" in xml2
        assert xml2 != xml1

    def test_version_bump_invalidates_snapshot(
        self, isolated_flowly_home, workspace_with_one_skill, monkeypatch,
    ) -> None:
        ws, _ = workspace_with_one_skill
        loader = _fresh_loader(ws)
        loader.build_skills_summary()
        snap_path = _skills_snapshot_path()
        assert snap_path.exists()

        # Manually pin an old version into the snapshot.
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        payload["version"] = 0  # any value != _SKILLS_SNAPSHOT_VERSION
        snap_path.write_text(json.dumps(payload), encoding="utf-8")

        _SKILLS_PROMPT_CACHE.clear()
        loader.build_skills_summary()

        # The old-version snapshot was discarded and a current-version
        # one written in its place.
        payload2 = json.loads(snap_path.read_text(encoding="utf-8"))
        assert payload2["version"] == _SKILLS_SNAPSHOT_VERSION

    def test_corrupt_snapshot_falls_back_to_cold_path(
        self, isolated_flowly_home, workspace_with_one_skill,
    ) -> None:
        ws, _ = workspace_with_one_skill
        loader = _fresh_loader(ws)
        loader.build_skills_summary()  # write snapshot

        # Corrupt the JSON.
        _skills_snapshot_path().write_text("{not json at all", encoding="utf-8")
        _SKILLS_PROMPT_CACHE.clear()

        # The cold path is silent on a corrupt snapshot — it just
        # rebuilds and overwrites. No exception escapes.
        xml = loader.build_skills_summary()
        assert "test-skill" in xml
        # Subsequent reads parse cleanly (the rebuild fixed it).
        payload = json.loads(_skills_snapshot_path().read_text(encoding="utf-8"))
        assert payload["version"] == _SKILLS_SNAPSHOT_VERSION


# ---------------------------------------------------------------------------
# Filter applied at LRU layer (snapshot is filter-agnostic)
# ---------------------------------------------------------------------------


class TestFilterAtLruLayer:
    """A single snapshot should serve every value of ``available_tools``.

    The filter (``requires_tools`` / ``fallback_for_tools``) is
    re-applied at the LRU rendering step, so the snapshot stays
    valid even when the agent toggles tools mid-session.
    """

    def test_requires_tools_filter_uses_cached_entries(
        self, isolated_flowly_home, tmp_path,
    ) -> None:
        ws = tmp_path / "ws"
        # Two skills: one unconditional, one that requires `browser_tab`.
        # The flowly-metadata frontmatter is parsed as JSON, so the
        # ``requires_tools`` list must use double quotes — a stray
        # Python repr (``['x']``) would silently parse to {} and
        # disable the filter, hiding a regression.
        for name, requires_json in [
            ("plain", "[]"),
            ("browser", '["browser_tab"]'),
        ]:
            d = ws / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(dedent(f"""\
                ---
                name: {name}
                description: {name} skill
                metadata: {{"flowly":{{"requires_tools":{requires_json}}}}}
                ---
                # {name}
            """), encoding="utf-8")

        loader = _fresh_loader(ws)

        # Filter active but `browser_tab` is NOT in the available
        # set — only the unconditional `plain` skill should render.
        # Note: passing ``available_tools=set()`` (truly empty) makes
        # ``if available_tools`` falsy and bypasses the filter; that
        # is a pre-existing Flowly semantic we leave unchanged here.
        xml_other_tool = loader.build_skills_summary(
            available_tools={"some_other_tool"}
        )
        assert "<name>plain</name>" in xml_other_tool
        assert "<name>browser</name>" not in xml_other_tool

        # With `browser_tab` available → both show. The snapshot was
        # written on the first call and reused; the filter swap happens
        # at LRU render time.
        xml_with_browser = loader.build_skills_summary(
            available_tools={"browser_tab"}
        )
        assert "<name>plain</name>" in xml_with_browser
        assert "<name>browser</name>" in xml_with_browser

        # The cold path ran exactly once: snapshot exists and only
        # one was written (we can't directly count writes but the file
        # mtime should not have advanced past the first call's tick).
        assert _skills_snapshot_path().exists()
