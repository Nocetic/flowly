"""Textual pilot test for the MCP modal (A2).

Mounts MCPModal in a headless Textual app and asserts it composes +
populates its list without crashing — catches compose()/CSS/_rebuild_list
errors that a plain import can't (the kind of bug that bit channels_list).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import OptionList  # noqa: E402

from flowly.config.loader import save_config  # noqa: E402
from flowly.config.schema import Config, MCPServerConfig  # noqa: E402


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    cfg = Config()
    cfg.mcp_servers = {"mine": MCPServerConfig(command="echo")}
    save_config(cfg)
    return tmp_path


class _Host(App):
    async def on_mount(self) -> None:
        from flowly.tui.panes.mcp_modal import MCPModal
        await self.push_screen(MCPModal())

    def compose(self) -> ComposeResult:  # pragma: no cover — empty base
        return []


@pytest.mark.asyncio
async def test_modal_mounts_and_lists_servers(isolated_home):
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = app.screen.query_one(OptionList)
        # The configured server + the catalog entries should all render as
        # option rows (no crash in compose/_rebuild_list).
        labels = "\n".join(str(o.prompt) for o in ol.options)
        assert "mine" in labels                 # configured server
        assert "context7" in labels             # catalog entry
        # No leftover conflict/■ rendering — rows are non-empty.
        assert len(ol.options) >= 2


@pytest.mark.asyncio
async def test_modal_reload_action(isolated_home):
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        modal.action_reload()       # must not raise
        await pilot.pause()
        ol = app.screen.query_one(OptionList)
        assert len(ol.options) >= 2


class _SecretHost(App):
    captured: dict | None = "UNSET"  # type: ignore[assignment]

    async def on_mount(self) -> None:
        from flowly.tui.panes.mcp_modal import MCPSecretModal
        from flowly.integrations.mcp_io import MCPSecretField
        fields = [MCPSecretField(name="API_TOKEN", prompt="API token", secret=True, default="")]

        def _done(result):
            self.captured = result
        await self.push_screen(MCPSecretModal("svc", fields), _done)

    def compose(self) -> ComposeResult:  # pragma: no cover
        return []


@pytest.mark.asyncio
async def test_secret_modal_collects_and_returns_values(isolated_home):
    from textual.widgets import Input
    app = _SecretHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.screen.query_one("#sf-API_TOKEN", Input)
        inp.value = "secret-123"
        await pilot.pause()
        app.screen._save()           # press Install
        await pilot.pause()
    assert app.captured == {"API_TOKEN": "secret-123"}


@pytest.mark.asyncio
async def test_secret_modal_cancel_returns_none(isolated_home):
    app = _SecretHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.action_cancel()
        await pilot.pause()
    assert app.captured is None


@pytest.mark.asyncio
async def test_secret_catalog_install_returns_inline_request(isolated_home, monkeypatch):
    from flowly.integrations.mcp_io import MCPSecretField, MCPServerEntry
    from flowly.tui.panes import mcp_modal as modal_mod

    field = MCPSecretField(
        name="API_TOKEN",
        prompt="API token",
        secret=True,
        default="",
    )
    entry = MCPServerEntry(
        name="svc",
        transport="stdio: svc",
        enabled=False,
        auth="",
        tool_filter="all",
        source="catalog",
        description="service",
        status="available",
        secret_fields=[field],
    )
    monkeypatch.setattr(modal_mod, "list_mcp_servers", lambda: [entry])

    captured: dict | None = None

    class _InlineInstallHost(App):
        async def on_mount(self) -> None:
            def _done(result):
                nonlocal captured
                captured = result

            await self.push_screen(modal_mod.MCPModal(), _done)

        def compose(self) -> ComposeResult:  # pragma: no cover
            return []

    app = _InlineInstallHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert captured == {
        "action": "install_secret",
        "name": "svc",
        "fields": [field],
    }
