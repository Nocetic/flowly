from types import SimpleNamespace

import pytest
import typer


def test_bare_flowly_without_provider_runs_onboarding(monkeypatch):
    import flowly.cli.commands as commands
    import flowly.cli.onboard_cmd as onboard_cmd
    import flowly.config.loader as loader
    import flowly.integrations.active_provider as active_provider

    calls: list[str] = []

    monkeypatch.setattr(loader, "load_config", lambda: object())
    monkeypatch.setattr(active_provider, "resolve_active_provider", lambda _cfg: None)
    monkeypatch.setattr(onboard_cmd, "run_onboarding", lambda: calls.append("onboarding"))

    with pytest.raises(typer.Exit) as exc:
        commands.main(
            SimpleNamespace(invoked_subcommand=None),
            version=False,
            host="127.0.0.1",
            port=18790,
            session="",
            new=False,
            theme="",
        )

    assert exc.value.exit_code == 0
    assert calls == ["onboarding"]
