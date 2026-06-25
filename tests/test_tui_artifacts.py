from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from flowly.tui.panes.artifacts_modal import ArtifactsModal


class _ArtifactsApp(App[None]):
    def __init__(self, artifacts: list[dict[str, object]]) -> None:
        super().__init__()
        self._artifacts = artifacts

    def on_mount(self) -> None:
        self.push_screen(ArtifactsModal(self._artifacts))


@pytest.mark.asyncio
async def test_artifacts_modal_mounts_markdown_table_artifact() -> None:
    app = _ArtifactsApp(
        [
            {
                "title": "table-report",
                "type": "markdown",
                "content": (
                    "| Metric | Value | Notes |\n"
                    "|---|---:|---|\n"
                    "| Revenue growth | 12.5% | base case |\n"
                ),
            }
        ]
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.1)
        body = app.screen_stack[-1].query_one("#art-body", Static)

    assert body is not None
