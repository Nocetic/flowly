from __future__ import annotations

import pytest

from flowly.tui import artifact_open


def _html_artifact() -> dict[str, object]:
    return {
        "id": "art-1",
        "type": "html",
        "version": 2,
        "content": "<html><body>hello</body></html>",
    }


def test_linux_without_display_reports_headless_without_writing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(artifact_open.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.setattr(artifact_open, "_is_wsl", lambda: False)
    monkeypatch.setattr(artifact_open, "get_data_dir", lambda: tmp_path)

    result = artifact_open.open_artifact_external(_html_artifact())

    assert result.status == "headless"
    assert not (tmp_path / "tmp" / "artifacts").exists()


def test_linux_desktop_materializes_and_opens_html(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    class _Process:
        pass

    def fake_popen(command, **kwargs):
        calls.append(command)
        return _Process()

    monkeypatch.setattr(artifact_open.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(artifact_open, "_is_wsl", lambda: False)
    monkeypatch.setattr(artifact_open.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(artifact_open, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(artifact_open.subprocess, "Popen", fake_popen)

    result = artifact_open.open_artifact_external(_html_artifact())

    assert result.status == "opened"
    assert result.path is not None
    assert result.path.read_text(encoding="utf-8") == "<html><body>hello</body></html>"
    assert calls == [["/usr/bin/xdg-open", str(result.path)]]


def test_artifact_open_screen_runs_in_a_textual_worker() -> None:
    """push_screen_wait may only run inside a worker; the ArtifactOpen
    handler must stay a plain delegator and the screen-opening method a
    @work-wrapped coroutine (regression: NoActiveWorker on Enter)."""
    import inspect

    from flowly.tui.app import FlowlyTUI

    handler = FlowlyTUI._on_artifact_open
    assert not inspect.iscoroutinefunction(handler)

    worker_method = FlowlyTUI._open_artifact_screen
    # @work replaces the coroutine with a sync wrapper that spawns a Worker;
    # the original coroutine stays reachable via functools.wraps.
    assert not inspect.iscoroutinefunction(worker_method)
    assert inspect.iscoroutinefunction(inspect.unwrap(worker_method))


@pytest.mark.asyncio
async def test_enter_opens_artifact_modal_end_to_end(monkeypatch) -> None:
    """Drive the real FlowlyTUI: ↓ selects the chat's artifact, Enter must
    open the viewer modal, Esc must close it back to the input
    (regression: NoActiveWorker from push_screen_wait in a handler)."""
    import asyncio

    from flowly.tui import app as app_mod
    from flowly.tui.panes.artifacts_modal import ArtifactsModal
    from flowly.tui.panes.composer import Composer

    monkeypatch.setattr(app_mod, "load_state", lambda: {})
    monkeypatch.setattr(app_mod, "save_state", lambda state: None)

    full_artifact = {
        "id": "art-1",
        "type": "markdown",
        "title": "Report",
        "content": "# hello",
        "session_key": "cli:e2e",
    }

    class _FakeClient:
        _url = "http://127.0.0.1:1"

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def events(self):
            await asyncio.Event().wait()
            yield None  # pragma: no cover

        async def artifacts_get(self, artifact_id: str):
            assert artifact_id == "art-1"
            return dict(full_artifact)

        async def artifacts_list(self, **kwargs):
            return []

        async def approval_list(self):
            return []

        async def chat_history(self, *args, **kwargs):
            return []

        def __getattr__(self, name: str):
            async def _generic(*args, **kwargs):
                return None

            return _generic

    app = app_mod.FlowlyTUI(client=_FakeClient(), session_key="cli:e2e")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        composer = app.query_one(Composer)
        composer.set_artifacts(
            [
                {
                    "id": "art-1",
                    "type": "markdown",
                    "title": "Report",
                    "updated_at": 1.0,
                    "session_key": "cli:e2e",
                }
            ]
        )
        composer.focus_input()

        await pilot.press("down")
        await pilot.pause()
        assert composer.artifact_navigation_active() is True

        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen_stack[-1], ArtifactsModal)

        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen_stack[-1], ArtifactsModal)
