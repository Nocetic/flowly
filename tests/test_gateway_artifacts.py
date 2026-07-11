from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from flowly.artifacts.store import ArtifactStore
from flowly.gateway.server import GatewayServer


@pytest.mark.asyncio
async def test_gateway_lists_session_artifact_summaries_without_content(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts.sqlite")
    try:
        first = store.create(
            type="html",
            title="First report",
            content="<html>first</html>",
            session_key="tui:first",
        )
        store.create(
            type="html",
            title="Second report",
            content="<html>second</html>",
            session_key="tui:second",
        )
        server = GatewayServer(
            host="127.0.0.1",
            port=0,
            on_chat_message=AsyncMock(),
            artifact_store=store,
        )
        replies: list[dict] = []

        async def capture_reply(ws, rpc_id, result):
            replies.append(result)

        server._ws_rpc_reply = capture_reply  # type: ignore[method-assign]
        await server._ws_rpc_artifacts_list(
            object(),  # type: ignore[arg-type]
            "rpc-1",
            {
                "sessionKey": "tui:first",
                "includeContent": False,
                "limit": 20,
            },
        )

        artifacts = replies[0]["artifacts"]
        assert [artifact["id"] for artifact in artifacts] == [first["id"]]
        assert artifacts[0]["session_key"] == "tui:first"
        assert "content" not in artifacts[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_gateway_create_preserves_session_key(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts.sqlite")
    try:
        server = GatewayServer(
            host="127.0.0.1",
            port=0,
            on_chat_message=AsyncMock(),
            artifact_store=store,
        )
        replies: list[dict] = []

        async def capture_reply(ws, rpc_id, result):
            replies.append(result)

        server._ws_rpc_reply = capture_reply  # type: ignore[method-assign]
        await server._ws_rpc_artifacts_create(
            object(),  # type: ignore[arg-type]
            "rpc-1",
            {
                "type": "markdown",
                "title": "Report",
                "content": "Body",
                "sessionKey": "tui:one",
            },
        )

        assert replies[0]["artifact"]["session_key"] == "tui:one"
    finally:
        store.close()
