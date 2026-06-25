"""Regression: the `flowly setup` command-execution wizard (``setup_exec``)
must persist the chosen security/ask policy to the exec-approvals STORE
(``credentials/exec-approvals.json``) — the single source of truth the
executor actually obeys (``executor.execute_command`` reads ``store.config``).

Historically the wizard wrote ``config.json`` ``tools.exec.security``/``ask``
instead, which *nothing* reads at runtime, so the wizard was a silent no-op:
the user picked "allowlist"/"always" and the gate still ran on the store's
defaults ("full"/"off"). These tests pin the wizard to the store.

``tools.exec.enabled`` legitimately lives in config.json (the runtime builds
``ExecConfig`` from it), so that one is asserted on config.json.
"""

import json

import pytest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Pin FLOWLY_HOME so both config.json and the approval store land in a
    throwaway dir, and neutralise the best-effort gateway bounce."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    import flowly.cli.setup as setup_mod

    monkeypatch.setattr(
        setup_mod, "_apply_changes_to_running_gateway", lambda *_a, **_k: None
    )
    return home


def _drive(monkeypatch, *, confirms, prompts):
    """Feed scripted answers to the wizard's Confirm.ask / Prompt.ask calls."""
    import flowly.cli.setup as setup_mod

    confirm_it = iter(confirms)
    prompt_it = iter(prompts)
    monkeypatch.setattr(setup_mod.Confirm, "ask", lambda *a, **k: next(confirm_it))
    monkeypatch.setattr(setup_mod.Prompt, "ask", lambda *a, **k: next(prompt_it))


def test_wizard_persists_allowlist_always_to_store(isolated_home, monkeypatch):
    from flowly.cli.setup import setup_exec
    from flowly.exec.approvals import ExecApprovalStore

    # exec is enabled by default → wizard asks "Reconfigure?" first.
    # Path: Reconfigure=yes, Enable=yes, security=allowlist(1), ask=always(2)
    _drive(monkeypatch, confirms=[True, True], prompts=["1", "2"])

    assert setup_exec() is True

    cfg = ExecApprovalStore().load()
    assert cfg.security == "allowlist"
    assert cfg.ask == "always"

    # enabled stays in config.json (the runtime reads it from there).
    saved = json.loads((isolated_home / "config.json").read_text())
    assert saved["tools"]["exec"]["enabled"] is True


def test_wizard_overwrites_existing_store(isolated_home, monkeypatch):
    from flowly.cli.setup import setup_exec
    from flowly.exec.approvals import ExecApprovalStore

    # Pre-seed the store with a non-default value so a pass can't be a
    # coincidence with the store's "full" default.
    seed = ExecApprovalStore()
    seed_cfg = seed.load()
    seed_cfg.security = "deny"
    seed.save()

    # Path: Reconfigure=yes, Enable=yes, security=full(2). No ask prompt for full.
    _drive(monkeypatch, confirms=[True, True], prompts=["2"])

    assert setup_exec() is True

    cfg = ExecApprovalStore().load()
    assert cfg.security == "full"
