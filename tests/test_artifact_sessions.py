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


@pytest.mark.asyncio
async def test_subagent_auto_saved_artifact_keeps_parent_session(tmp_path) -> None:
    from flowly.artifacts.store import _CACHE, get_store

    manager = object.__new__(SubagentManager)
    manager._state_dir = tmp_path
    manager._artifact_on_change = None

    artifact_id = await manager._save_result_as_artifact(
        "run-1",
        "builtin:writer",
        "Write a report",
        "Report body",
        session_key="tui:parent",
    )

    store = get_store(tmp_path)
    try:
        artifact = store.get(artifact_id or "")
        assert artifact is not None
        assert artifact["session_key"] == "tui:parent"
    finally:
        store.close()
        _CACHE.pop(str(tmp_path), None)
