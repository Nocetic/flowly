from __future__ import annotations

import pytest

from flowly.agent.subagent import SubagentManager
from flowly.artifacts.store import ArtifactStore
from flowly.artifacts.summary import artifact_summary


def test_artifact_store_filters_by_session_with_and_without_search(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts.sqlite")
    try:
        first = store.create(
            type="markdown",
            title="Shared report",
            content="revenue alpha",
            session_key="tui:first",
        )
        store.create(
            type="markdown",
            title="Shared report",
            content="revenue beta",
            session_key="tui:second",
        )
        store.create(
            type="markdown",
            title="Legacy report",
            content="revenue legacy",
        )

        assert [item["id"] for item in store.list(session_key="tui:first")] == [
            first["id"]
        ]
        assert [
            item["id"]
            for item in store.list(search="revenue", session_key="tui:first")
        ] == [first["id"]]
    finally:
        store.close()


def test_artifact_summary_keeps_session_and_omits_content() -> None:
    summary = artifact_summary(
        {
            "id": "art-1",
            "type": "html",
            "title": "Report",
            "content": "<html>large</html>",
            "session_key": "tui:one",
            "metadata": {"language": "html"},
        }
    )

    assert summary["session_key"] == "tui:one"
    assert summary["metadata"] == {"language": "html"}
    assert "content" not in summary
