"""Tests for the gateway's ``commands.list`` RPC handler.

The handler enumerates slash command categories — built-ins, plugin
slash commands, skill bundles, and individual skills — so the desktop
composer can power a ``/`` autocomplete dropdown. These tests pin down
the contract: shape of the response, alphabetical sort, plugin/bundle
failure tolerance, bundle skill_count surface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, AsyncMock

import pytest

from flowly.gateway.server import GatewayServer


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fresh_server() -> tuple[GatewayServer, list[dict]]:
    """Construct a GatewayServer wired with minimum collaborators.

    ``replies`` accumulates whatever the handler sends via
    ``_ws_rpc_reply`` so tests can inspect the payload without
    spinning up a real WebSocket.
    """
    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        on_voice_message=AsyncMock(),
        on_cron_run=AsyncMock(),
        on_cron_health=AsyncMock(),
        on_cron_reload=AsyncMock(),
        on_chat_message=AsyncMock(),
        sessions=MagicMock(),
        subagent_registry=None,
        artifact_store=None,
        on_compact=AsyncMock(),
        on_clear=AsyncMock(),
    )

    replies: list[dict] = []

    async def fake_reply(ws, rpc_id, result):
        replies.append({"rpc_id": rpc_id, "result": result})

    server._ws_rpc_reply = fake_reply  # type: ignore[method-assign]
    return server, replies


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the bundles directory at a tmp dir for the duration of the test."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.agent import skill_bundles
    skill_bundles.reload()
    yield tmp_path
    skill_bundles.reload()


def _write_skill(home: Path, name: str, body: str = "skill body") -> Path:
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_returns_builtin_commands(isolated_home: Path, monkeypatch):
    """The hardcoded slash commands are always present.

    Conversation lifecycle (help/compact/clear/new) + read-only
    introspection/runtime commands. Pin all of them so accidentally
    dropping one in a future refactor fails CI.
    """
    # Stub the plugin manager away so nothing else contributes.
    import flowly.plugins as plugins_mod

    def _no_manager():
        raise RuntimeError("plugin manager not initialised")

    monkeypatch.setattr(plugins_mod, "get_plugin_manager", _no_manager)

    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "rpc-1", {}))

    assert len(replies) == 1
    result = replies[0]["result"]
    names = {b["name"] for b in result["builtin"]}
    assert names == {
        "help", "compact", "clear", "new", "retry", "undo",
        "skills", "learn", "whoami", "status", "codex",
    }
    # Every entry has a non-empty description.
    for entry in result["builtin"]:
        assert entry["description"]


def test_builtin_list_is_well_formed(isolated_home: Path, monkeypatch):
    """Each builtin has 'name' (str) and 'description' (str)."""
    monkeypatch.setattr(
        "flowly.plugins.get_plugin_manager",
        lambda: (_ for _ in ()).throw(RuntimeError),
    )
    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    for entry in replies[0]["result"]["builtin"]:
        assert isinstance(entry["name"], str) and entry["name"]
        assert isinstance(entry["description"], str)


def test_plugin_commands_alphabetical(isolated_home: Path, monkeypatch):
    """Plugin slash commands come out sorted, even if registered out of order."""
    fake_manager = MagicMock()
    fake_manager._slash_commands = {
        "zoom-out": {"description": "Reset zoom"},
        "alpha": {"description": "First thing"},
        "middle": {"description": "Middle thing"},
    }
    monkeypatch.setattr("flowly.plugins.get_plugin_manager", lambda: fake_manager)

    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    names = [p["name"] for p in replies[0]["result"]["plugin"]]
    assert names == ["alpha", "middle", "zoom-out"]


def test_plugin_section_empty_when_no_manager(isolated_home: Path, monkeypatch):
    """A missing/broken plugin manager doesn't break the call."""
    monkeypatch.setattr(
        "flowly.plugins.get_plugin_manager",
        lambda: (_ for _ in ()).throw(RuntimeError("not wired")),
    )
    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    assert replies[0]["result"]["plugin"] == []


def test_plugin_section_handles_missing_descriptions(isolated_home: Path, monkeypatch):
    """An entry without a description still serialises cleanly."""
    fake_manager = MagicMock()
    fake_manager._slash_commands = {
        "no-desc": {},
        "with-desc": {"description": "yes"},
    }
    monkeypatch.setattr("flowly.plugins.get_plugin_manager", lambda: fake_manager)

    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    entries = {p["name"]: p["description"] for p in replies[0]["result"]["plugin"]}
    assert entries == {"no-desc": "", "with-desc": "yes"}


def test_bundles_surface_with_skill_count(isolated_home: Path, monkeypatch):
    """A bundle file on disk shows up in the bundle section."""
    monkeypatch.setattr(
        "flowly.plugins.get_plugin_manager",
        lambda: (_ for _ in ()).throw(RuntimeError),
    )

    from flowly.agent import skill_bundles
    skill_bundles.save_bundle(
        name="Research",
        skills=["github", "plan", "test-driven-development"],
        description="Research workflow",
    )

    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    bundles = replies[0]["result"]["bundle"]
    assert len(bundles) == 1
    entry = bundles[0]
    assert entry["name"] == "research"  # slug, no leading slash
    assert entry["description"] == "Research workflow"
    assert entry["skill_count"] == 3


def test_bundles_sorted_alphabetically(isolated_home: Path, monkeypatch):
    monkeypatch.setattr(
        "flowly.plugins.get_plugin_manager",
        lambda: (_ for _ in ()).throw(RuntimeError),
    )

    from flowly.agent import skill_bundles
    skill_bundles.save_bundle(name="Zebra", skills=["one"])
    skill_bundles.save_bundle(name="Alpha", skills=["one"])
    skill_bundles.save_bundle(name="Middle", skills=["one", "two"])

    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    names = [b["name"] for b in replies[0]["result"]["bundle"]]
    assert names == ["alpha", "middle", "zebra"]


def test_bundle_section_empty_when_dir_missing(isolated_home: Path, monkeypatch):
    """Fresh profile with no bundles dir returns empty bundle list (not error)."""
    monkeypatch.setattr(
        "flowly.plugins.get_plugin_manager",
        lambda: (_ for _ in ()).throw(RuntimeError),
    )
    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    assert replies[0]["result"]["bundle"] == []


def test_response_shape_has_command_categories(isolated_home: Path, monkeypatch):
    """Every response carries the same top-level keys, regardless of contents."""
    monkeypatch.setattr(
        "flowly.plugins.get_plugin_manager",
        lambda: (_ for _ in ()).throw(RuntimeError),
    )
    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    result = replies[0]["result"]
    assert set(result.keys()) == {"builtin", "plugin", "bundle", "skill", "skill_hidden"}
    assert isinstance(result["builtin"], list)
    assert isinstance(result["plugin"], list)
    assert isinstance(result["bundle"], list)
    assert isinstance(result["skill"], list)
    assert isinstance(result["skill_hidden"], int)


def test_full_payload_with_all_categories(isolated_home: Path, monkeypatch):
    """End-to-end: all categories populated, all sorted."""
    fake_manager = MagicMock()
    fake_manager._slash_commands = {
        "lint": {"description": "Run lint"},
    }
    monkeypatch.setattr("flowly.plugins.get_plugin_manager", lambda: fake_manager)

    from flowly.agent import skill_bundles
    skill_bundles.save_bundle(name="Coding", skills=["a", "b"])
    _write_skill(isolated_home, "alpha-skill")

    server, replies = _fresh_server()
    _run(server._ws_rpc_commands_list(MagicMock(), "x", {}))
    result = replies[0]["result"]
    assert {b["name"] for b in result["builtin"]} == {
        "help", "compact", "clear", "new", "retry", "undo",
        "skills", "learn", "whoami", "status", "codex",
    }
    assert [p["name"] for p in result["plugin"]] == ["lint"]
    assert [b["name"] for b in result["bundle"]] == ["coding"]
    assert "alpha-skill" in {s["name"] for s in result["skill"]}
