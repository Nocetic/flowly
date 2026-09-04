"""MCP rich-content fidelity without raw base64 context expansion."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from mcp import types

from flowly.mcp.tool import MCPTool


def _tool(max_binary_bytes: int = 1024 * 1024) -> MCPTool:
    task = SimpleNamespace(name="rich", max_binary_bytes=max_binary_bytes)
    remote = types.Tool(
        name="inspect",
        title="Inspect result",
        description="Return rich content",
        inputSchema={"type": "object"},
        outputSchema={"type": "object"},
        annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
        icons=[types.Icon(src="https://example.invalid/icon.png", theme="dark")],
        _meta={"vendor": "test"},
    )
    return MCPTool(server_task=task, remote_tool=remote)


def test_tool_definition_retains_protocol_metadata() -> None:
    tool = _tool()
    assert tool.title == "Inspect result"
    assert tool.output_schema == {"type": "object"}
    assert tool.annotations["readOnlyHint"] is True
    assert tool.annotations["idempotentHint"] is True
    assert tool.icons == [{"src": "https://example.invalid/icon.png", "theme": "dark"}]
    assert tool.mcp_metadata == {"vendor": "test"}


def test_all_standard_content_types_are_preserved(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    image = base64.b64encode(b"png-bytes").decode()
    audio = base64.b64encode(b"wave-bytes").decode()
    blob = base64.b64encode(b"binary-document").decode()

    result = types.CallToolResult(
        content=[
            types.TextContent(
                text="hello",
                annotations=types.Annotations(audience=["assistant"], priority=0.8),
                _meta={"text-key": "text-value"},
            ),
            types.ImageContent(data=image, mimeType="image/png"),
            types.AudioContent(data=audio, mimeType="audio/wav"),
            types.ResourceLink(
                name="manual",
                title="Manual",
                uri="docs://manual",
                description="Product manual",
                mimeType="text/markdown",
                size=42,
                icons=[types.Icon(src="data:image/png;base64,AA==", sizes=["16x16"])],
                _meta={"resource-key": 1},
            ),
            types.EmbeddedResource(
                resource=types.TextResourceContents(
                    uri="memory://note",
                    mimeType="text/plain",
                    text="embedded text",
                    _meta={"revision": 3},
                ),
            ),
            types.EmbeddedResource(
                resource=types.BlobResourceContents(
                    uri="file://report.bin",
                    mimeType="application/octet-stream",
                    blob=blob,
                ),
            ),
        ],
        structuredContent={"answer": 42},
        _meta={"trace": "abc"},
        resultType="input_required",
    )

    payload = json.loads(_tool()._format_result(result))
    assert payload["structuredContent"] == {"answer": 42}
    assert payload["_meta"] == {"trace": "abc"}
    assert payload["resultType"] == "input_required"
    assert "hello" in payload["result"]
    assert "embedded text" in payload["result"]

    blocks = payload["content"]
    assert [block["type"] for block in blocks] == [
        "text",
        "image",
        "audio",
        "resource_link",
        "resource",
        "resource",
    ]
    assert blocks[0]["annotations"]["audience"] == ["assistant"]
    assert blocks[0]["_meta"] == {"text-key": "text-value"}
    assert blocks[3]["icons"][0]["sizes"] == ["16x16"]
    assert blocks[3]["_meta"] == {"resource-key": 1}
    assert blocks[4]["resource"]["_meta"] == {"revision": 3}
    assert blocks[5]["resource"]["size"] == len(b"binary-document")

    cached_paths = [
        Path(block["path"])
        for block in blocks
        if block["type"] in {"image", "audio"}
    ]
    cached_paths.append(Path(blocks[5]["resource"]["path"]))
    assert all(path.exists() for path in cached_paths)
    assert not any("data" in block for block in blocks)
    assert "png-bytes" not in json.dumps(payload)


def test_oversized_media_is_reported_not_written(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    result = types.CallToolResult(content=[
        types.AudioContent(
            data=base64.b64encode(b"too large").decode(),
            mimeType="audio/wav",
        ),
    ])

    payload = json.loads(_tool(max_binary_bytes=4)._format_result(result))
    assert "unavailable" in payload["content"][0]
    assert not (tmp_path / "media" / "mcp").exists()


def test_protocol_error_stays_machine_detectable() -> None:
    result = types.CallToolResult(
        content=[types.TextContent(text="invalid request")],
        isError=True,
    )
    payload = json.loads(_tool()._format_result(result))
    assert payload == {"error": "invalid request", "isError": True}
