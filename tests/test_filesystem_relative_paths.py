from __future__ import annotations

from pathlib import Path

from flowly.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    _resolve_tool_path,
)


def test_relative_path_resolution_uses_workspace_not_process_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    process_cwd = tmp_path / "gateway-cwd"
    workspace.mkdir()
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)

    assert _resolve_tool_path("HEARTBEAT.md", workspace) == (
        workspace / "HEARTBEAT.md"
    ).resolve()


def test_absolute_path_resolution_is_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    process_cwd = tmp_path / "gateway-cwd"
    absolute = tmp_path / "absolute.txt"
    workspace.mkdir()
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)

    assert _resolve_tool_path(str(absolute), workspace) == absolute.resolve()


async def test_relative_read_and_list_ignore_gateway_process_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    process_cwd = tmp_path / "gateway-cwd"
    workspace.mkdir()
    process_cwd.mkdir()
    (workspace / "HEARTBEAT.md").write_text("workspace copy", encoding="utf-8")
    (process_cwd / "HEARTBEAT.md").write_text("wrong process copy", encoding="utf-8")
    (workspace / "notes").mkdir()
    (workspace / "notes" / "today.md").write_text("note", encoding="utf-8")
    monkeypatch.chdir(process_cwd)

    read_result = await ReadFileTool(workspace=workspace).execute("HEARTBEAT.md")
    list_result = await ListDirTool(workspace=workspace).execute("notes")

    assert read_result == "workspace copy"
    assert "today.md" in list_result


async def test_relative_write_and_edit_target_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    process_cwd = tmp_path / "gateway-cwd"
    workspace.mkdir()
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)

    write_result = await WriteFileTool(workspace=workspace).execute(
        "notes.txt",
        "before",
    )
    edit_result = await EditFileTool(workspace=workspace).execute(
        "notes.txt",
        "before",
        "after",
    )

    assert write_result.startswith("Successfully wrote")
    assert edit_result.startswith("Successfully edited")
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "after"
    assert not (process_cwd / "notes.txt").exists()


async def test_relative_write_cannot_escape_workspace_with_parent_traversal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    escaped = tmp_path / "escaped.txt"

    result = await WriteFileTool(workspace=workspace).execute(
        "../escaped.txt",
        "must not be written",
    )

    assert result.startswith("Error: write access denied")
    assert not escaped.exists()


async def test_relative_write_cannot_escape_workspace_through_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    result = await WriteFileTool(workspace=workspace).execute(
        "linked/escaped.txt",
        "must not be written",
    )

    assert result.startswith("Error: write access denied")
    assert not (outside / "escaped.txt").exists()
