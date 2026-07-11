from __future__ import annotations

import pytest
from rich.syntax import Syntax
from rich.table import Table
from textual.app import App
from textual.widgets import Static

from flowly.tui.client import ArtifactEvent, GatewayClient
from flowly.tui.panes.artifacts_modal import ArtifactsModal, _render_artifact


class _ArtifactsApp(App[None]):
    def __init__(self, artifacts: list[dict[str, object]], **modal_kwargs: object) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._modal_kwargs = modal_kwargs

    def on_mount(self) -> None:
        self.push_screen(ArtifactsModal(self._artifacts, **self._modal_kwargs))


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


def test_terminal_renderer_handles_code_language_and_csv() -> None:
    code = _render_artifact(
        {
            "type": "code",
            "content": "print('hello')",
            "metadata": {"language": "python"},
        }
    )
    csv_table = _render_artifact(
        {"type": "csv", "content": "name,value\nalpha,1\nbeta,2\n"}
    )

    assert isinstance(code, Syntax)
    assert code.lexer.name == "Python"
    assert isinstance(csv_table, Table)


@pytest.mark.asyncio
async def test_gateway_client_surfaces_artifact_event_as_summary() -> None:
    client = GatewayClient()
    await client._dispatch(
        {
            "type": "event",
            "event": "artifact.created",
            "data": {
                "id": "art-1",
                "type": "html",
                "title": "Report",
                "content": "<html>large</html>",
                "session_key": "tui:one",
            },
        }
    )

    event = await client._inbox.get()
    assert isinstance(event, ArtifactEvent)
    assert event.action == "created"
    assert event.artifact["session_key"] == "tui:one"
    assert "content" not in event.artifact


@pytest.mark.asyncio
async def test_gateway_client_filters_old_gateway_global_artifact_response() -> None:
    client = GatewayClient()

    async def fake_rpc(method, params):
        assert method == "artifacts.list"
        assert params["sessionKey"] == "tui:one"
        assert params["includeContent"] is False
        return "rpc-1"

    async def fake_reply(rpc_id, timeout):
        return {
            "artifacts": [
                {
                    "id": "art-1",
                    "type": "html",
                    "content": "large",
                    "session_key": "tui:one",
                },
                {
                    "id": "art-2",
                    "type": "html",
                    "content": "other",
                    "session_key": "tui:two",
                },
            ]
        }

    client._rpc = fake_rpc  # type: ignore[method-assign]
    client._await_reply = fake_reply  # type: ignore[method-assign]
    artifacts = await client.artifacts_list(
        session_key="tui:one",
        include_content=False,
    )

    assert [artifact["id"] for artifact in artifacts] == ["art-1"]
    assert "content" not in artifacts[0]


def _three_markdown_artifacts() -> list[dict[str, object]]:
    return [
        {"id": "a1", "title": "one", "type": "markdown", "content": "# one"},
        {"id": "a2", "title": "two", "type": "markdown", "content": "# two"},
        {"id": "a3", "title": "three", "type": "markdown", "content": "# three"},
    ]


@pytest.mark.asyncio
async def test_artifacts_modal_left_right_cycles_selection() -> None:
    from textual.widgets import ListView

    app = _ArtifactsApp(_three_markdown_artifacts())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        list_view = app.screen_stack[-1].query_one("#artifact-list", ListView)
        assert list_view.index == 0

        await pilot.press("right")
        await pilot.pause()
        assert list_view.index == 1

        await pilot.press("left")
        await pilot.press("left")
        await pilot.pause()
        assert list_view.index == 2  # wraps past the first item


@pytest.mark.asyncio
async def test_artifacts_modal_initial_index_opens_that_artifact() -> None:
    from textual.widgets import ListView

    app = _ArtifactsApp(_three_markdown_artifacts(), initial_index=2)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        list_view = app.screen_stack[-1].query_one("#artifact-list", ListView)
        assert list_view.index == 2


@pytest.mark.asyncio
async def test_artifacts_modal_lazy_loads_summary_content_once() -> None:
    fetched: list[str] = []

    async def fetcher(artifact_id: str) -> dict[str, object]:
        fetched.append(artifact_id)
        return {
            "id": artifact_id,
            "type": "markdown",
            "content": f"# fetched {artifact_id}",
        }

    artifacts: list[dict[str, object]] = [
        {"id": "a1", "title": "one", "type": "markdown", "content": "# ready"},
        {"id": "a2", "title": "two", "type": "markdown"},  # summary, no content
    ]
    app = _ArtifactsApp(artifacts, fetcher=fetcher)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert fetched == []  # the pre-loaded artifact needs no fetch

        await pilot.press("right")
        await pilot.pause(0.1)
        assert fetched == ["a2"]
        assert artifacts[1]["content"] == "# fetched a2"  # cached in place

        await pilot.press("left")
        await pilot.press("right")
        await pilot.pause(0.1)
        assert fetched == ["a2"]  # revisits render from the cache


@pytest.mark.asyncio
async def test_artifacts_modal_lazy_loads_initial_summary_content() -> None:
    fetched: list[str] = []

    async def fetcher(artifact_id: str) -> dict[str, object]:
        fetched.append(artifact_id)
        return {
            "id": artifact_id,
            "type": "markdown",
            "content": "# loaded initial artifact",
        }

    artifacts: list[dict[str, object]] = [
        {"id": "a1", "title": "one", "type": "markdown"},
    ]
    app = _ArtifactsApp(artifacts, fetcher=fetcher)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)

        assert fetched == ["a1"]
        assert artifacts[0]["content"] == "# loaded initial artifact"
