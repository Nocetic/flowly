"""Tests for the `/codex` slash command handler (AgentLoop methods).

Exercises the config-mutation + live-sync flow of ``_handle_codex_command``
without constructing a full AgentLoop: the relevant methods are bound onto
a lightweight stub that supplies a real Config + a fake tool registry, and
the disk-write / heavy-registration steps are stubbed.
"""

from __future__ import annotations

import types

import pytest

from flowly.agent.loop import AgentLoop
from flowly.config.schema import Config


class _FakeRegistry:
    def __init__(self) -> None:
        self._names: set[str] = set()

    def has(self, name: str) -> bool:
        return name in self._names

    def unregister(self, name: str) -> None:
        self._names.discard(name)

    def register_name(self, name: str) -> None:
        self._names.add(name)


def _make_loop():
    loop = types.SimpleNamespace()
    loop._main_config = Config()
    loop.tools = _FakeRegistry()
    loop._codex_sessions = {}
    # Bind the real methods under test.
    for name in (
        "_handle_codex_command",
        "sync_codex_session_tool",
        "_close_warm_codex_sessions",
        "_format_codex_status",
    ):
        setattr(loop, name, types.MethodType(getattr(AgentLoop, name), loop))
    # Stub the disk + heavy-registration side effects.
    loop._persist_codex_config = lambda: None
    loop._register_codex_session_tool = lambda: None
    return loop


@pytest.mark.asyncio
async def test_status_default():
    loop = _make_loop()
    out = await loop._handle_codex_command("")
    assert "Codex runtime" in out
    assert "Status:" in out


@pytest.mark.asyncio
async def test_sandbox_set_persists_to_config():
    loop = _make_loop()
    out = await loop._handle_codex_command("sandbox full-access")
    assert loop._main_config.tools.codex_session.sandbox == "full-access"
    assert "full-access" in out


@pytest.mark.asyncio
async def test_cwd_set_expands_and_persists():
    import os
    loop = _make_loop()
    out = await loop._handle_codex_command("cwd ~/flowlyai")
    expected = os.path.abspath(os.path.expanduser("~/flowlyai"))
    assert loop._main_config.tools.codex_session.cwd == expected
    assert expected in out


@pytest.mark.asyncio
async def test_cwd_no_arg_shows_current():
    loop = _make_loop()
    loop._main_config.tools.codex_session.cwd = "/tmp/proj"
    out = await loop._handle_codex_command("cwd")
    assert "/tmp/proj" in out


@pytest.mark.asyncio
async def test_sandbox_invalid_rejected():
    loop = _make_loop()
    before = loop._main_config.tools.codex_session.sandbox
    out = await loop._handle_codex_command("sandbox nope")
    assert "Usage" in out
    assert loop._main_config.tools.codex_session.sandbox == before


@pytest.mark.asyncio
async def test_disable_clears_flag_and_warm_sessions():
    loop = _make_loop()
    loop._main_config.tools.codex_session.enabled = True

    class _WarmSess:
        def __init__(self) -> None:
            self.closed = False

        async def close(self):
            self.closed = True

    warm = _WarmSess()
    loop._codex_sessions["s1"] = warm
    out = await loop._handle_codex_command("off")
    assert loop._main_config.tools.codex_session.enabled is False
    assert warm.closed is True
    assert loop._codex_sessions == {}
    assert "disabled" in out.lower()


@pytest.mark.asyncio
async def test_tools_toggle():
    loop = _make_loop()
    out = await loop._handle_codex_command("tools off")
    assert loop._main_config.tools.codex_session.expose_flowly_tools is False
    assert "off" in out.lower()
    await loop._handle_codex_command("tools on")
    assert loop._main_config.tools.codex_session.expose_flowly_tools is True


@pytest.mark.asyncio
async def test_unknown_subcommand():
    loop = _make_loop()
    out = await loop._handle_codex_command("frobnicate")
    assert "Unknown" in out


@pytest.mark.asyncio
async def test_enable_requires_codex_cli(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    loop = _make_loop()
    out = await loop._handle_codex_command("on")
    assert loop._main_config.tools.codex_session.enabled is False
    assert "Codex CLI not found" in out


@pytest.mark.asyncio
async def test_enable_when_cli_present(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _bin: "/usr/local/bin/codex")
    loop = _make_loop()
    out = await loop._handle_codex_command("on")
    assert loop._main_config.tools.codex_session.enabled is True
    assert "enabled" in out.lower()
