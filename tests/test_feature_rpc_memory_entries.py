"""Tests for the gateway's ``memory.entries`` RPC handler.

``memory_entries`` feeds the desktop memory panel (remote mode). The
contract pinned here: date-stamped freeform blocks become entries, the
governance-generated sentinel region is excluded (its content is served
by ``memory.gov_list`` instead), and manual blocks around the region
survive in order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.channels import feature_rpc
from flowly.memory.summary import SENTINEL_END, SENTINEL_START


@pytest.fixture()
def workspace(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(feature_rpc, "workspace_dir", lambda: tmp_path)
    (tmp_path / "memory").mkdir(parents=True)
    return tmp_path


def _write_memory(workspace: Path, text: str) -> None:
    (workspace / "memory" / "MEMORY.md").write_text(text, encoding="utf-8")


def test_no_memory_file(workspace):
    out = feature_rpc.memory_entries()
    assert out == {"memory": [], "user": None}


def test_freeform_blocks_parse(workspace):
    _write_memory(
        workspace,
        "<!-- 2026-06-01 10:00 -->\nUser prefers dark mode.\n\n"
        "<!-- 2026-06-02 11:30 -->\nProject X ships Friday.\n",
    )
    out = feature_rpc.memory_entries()
    assert [e["date"] for e in out["memory"]] == ["2026-06-01 10:00", "2026-06-02 11:30"]
    assert out["memory"][0]["content"] == "User prefers dark mode."


def test_generated_region_excluded(workspace):
    _write_memory(
        workspace,
        "<!-- 2026-06-01 10:00 -->\nManual note before.\n\n"
        f"{SENTINEL_START}\n# Memory\n\n## Preferences\n- governed item\n{SENTINEL_END}\n\n"
        "<!-- 2026-06-03 09:00 -->\nManual note after.\n",
    )
    out = feature_rpc.memory_entries()
    contents = [e["content"] for e in out["memory"]]
    assert contents == ["Manual note before.", "Manual note after."]
    assert not any("governed item" in c for c in contents)


def test_only_generated_region(workspace):
    _write_memory(
        workspace,
        f"{SENTINEL_START}\n# Memory\n- governed only\n{SENTINEL_END}\n",
    )
    out = feature_rpc.memory_entries()
    assert out["memory"] == []


def test_unclosed_sentinel_treated_as_freeform(workspace):
    # A START without END is not a valid region — content stays visible
    # (comments themselves are stripped from the displayed content).
    _write_memory(
        workspace,
        f"<!-- 2026-06-01 10:00 -->\nNote.\n{SENTINEL_START}\ndangling\n",
    )
    out = feature_rpc.memory_entries()
    assert len(out["memory"]) == 1
    assert "dangling" in out["memory"][0]["content"]


def test_user_md_surfaced(workspace):
    (workspace / "USER.md").write_text("Hakan, engineer.", encoding="utf-8")
    out = feature_rpc.memory_entries()
    assert out["user"] == "Hakan, engineer."
