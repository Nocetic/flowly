"""Tests for the setup-home routing + modes (flowly/cli/onboard_cmd.py).

The interactive menu + section modals are mocked, so these assert the
orchestration (which mode runs which steps, and that the non-TTY guard still
short-circuits before any prompt) without touching InquirerPy/Textual.
"""

from __future__ import annotations

import types
from pathlib import Path

import flowly.cli.onboard_cmd as ob

# ── modes ────────────────────────────────────────────────────────────────

def test_blank_mode_skips_config_but_offers_gateway(monkeypatch):
    """Blank skips channels/integrations/media — but still offers the gateway.

    Without the offer, blank-mode users landed on "flowly → Gateway not
    reachable" and had to type `flowly service install --start` by hand
    (a real support case from a fresh VPS install).
    """
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "blank")
    monkeypatch.setattr(ob, "_run_provider_step", lambda: calls.append("provider") or True)
    monkeypatch.setattr(ob, "_configure_channels", lambda: calls.append("channels"))
    monkeypatch.setattr(ob, "_configure_tools", lambda: calls.append("tools"))
    monkeypatch.setattr(ob, "_configure_media", lambda: calls.append("media"))
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    assert calls == ["provider", "summary", "gateway"]  # no channels/tools/media


def test_full_mode_configures_channels_tools_media(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "full")
    monkeypatch.setattr(ob, "_run_provider_step", lambda: calls.append("provider") or True)
    monkeypatch.setattr(ob, "_configure_channels", lambda: calls.append("channels"))
    monkeypatch.setattr(ob, "_configure_tools", lambda: calls.append("tools"))
    monkeypatch.setattr(ob, "_configure_media", lambda: calls.append("media"))
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    assert calls == ["provider", "channels", "tools", "media", "summary", "gateway"]


def test_quick_mode_is_provider_then_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "quick")
    monkeypatch.setattr(ob, "_run_provider_step", lambda: calls.append("provider") or True)
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    assert calls == ["provider", "summary", "gateway"]


def test_onboarding_never_launches_textual_setup():
    """Guard: neither onboarding nor its inline card editor may launch the
    Textual setup screens (flowly.tui.setup_app)."""
    import inspect

    import flowly.cli.inline_cards as ic

    assert "flowly.tui.setup_app" not in inspect.getsource(ob)
    assert "flowly.tui.setup_app" not in inspect.getsource(ic)
    assert not hasattr(ob, "_open_section")


def test_backing_out_of_a_mode_returns_to_home(monkeypatch):
    calls = []
    menu = iter(["blank", "quit"])  # back out of blank → home re-shows → quit
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: next(menu))
    monkeypatch.setattr(ob, "_run_provider_step", lambda: False)  # user backed out
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    # mode aborted → no summary/gateway; home re-shown then quit (no crash/loop)
    assert calls == []


# ── provider-step routing (incl. the xAI OAuth browser flow) ─────────────

def test_provider_step_routes_flowly_then_model(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_onboarding_menu", lambda: "flowly")
    monkeypatch.setattr(ob, "_run_managed_login", lambda: calls.append("managed"))
    monkeypatch.setattr(ob, "_run_xai_oauth_login", lambda: calls.append("xai"))
    monkeypatch.setattr(ob, "_prompt_byok_key", lambda s: calls.append(f"byok:{s}"))
    monkeypatch.setattr(ob, "_prompt_model", lambda s: calls.append(f"model:{s}"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    assert ob._run_provider_step() is True
    # Flowly hosted also offers a (plan-filtered) model picker
    assert calls == ["managed", "model:flowly"]


def test_provider_step_routes_xai_oauth_then_model(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_onboarding_menu", lambda: "xai_oauth")
    monkeypatch.setattr(ob, "_run_managed_login", lambda: calls.append("managed"))
    monkeypatch.setattr(ob, "_run_xai_oauth_login", lambda: calls.append("xai"))
    monkeypatch.setattr(ob, "_prompt_byok_key", lambda s: calls.append(f"byok:{s}"))
    monkeypatch.setattr(ob, "_prompt_model", lambda s: calls.append(f"model:{s}"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    ob._run_provider_step()
    assert calls == ["xai", "model:xai_oauth"]


def test_provider_step_routes_zai_coding_then_model(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_onboarding_menu", lambda: "zai_coding")
    monkeypatch.setattr(ob, "_run_zai_coding_login", lambda: calls.append("glm"))
    monkeypatch.setattr(ob, "_prompt_byok_key", lambda s: calls.append(f"byok:{s}"))
    monkeypatch.setattr(ob, "_prompt_model", lambda s: calls.append(f"model:{s}"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    ob._run_provider_step()
    assert calls == ["glm", "model:zai_coding"]


def test_provider_step_routes_byok_slug(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_onboarding_menu", lambda: "zhipu")
    monkeypatch.setattr(ob, "_prompt_byok_key", lambda s: calls.append(f"byok:{s}") or True)
    monkeypatch.setattr(ob, "_prompt_model", lambda s: calls.append(f"model:{s}"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    ob._run_provider_step()
    assert calls == ["byok:zhipu", "model:zhipu"]


def test_provider_step_back_returns_false(monkeypatch):
    monkeypatch.setattr(ob, "_onboarding_menu", lambda: None)  # backed out
    assert ob._run_provider_step() is False


def test_byok_not_saved_skips_model(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_onboarding_menu", lambda: "anthropic")
    monkeypatch.setattr(ob, "_prompt_byok_key", lambda s: calls.append(f"key:{s}") or False)
    monkeypatch.setattr(ob, "_prompt_model", lambda s: calls.append(f"model:{s}"))
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    ob._run_provider_step()
    assert calls == ["key:anthropic"]  # no model picker when the key wasn't saved


def test_prompt_model_saves_selection(monkeypatch):
    from InquirerPy import inquirer as _inq

    import flowly.config.loader as loader
    import flowly.integrations.model_catalog as mc
    from flowly.config.schema import Config
    from flowly.integrations.model_catalog import Model

    async def _fake_fetch(slug, **k):
        return [Model(id="anthropic/claude-x", name="Claude X")]

    class _Fuzzy:
        def execute(self):
            return "anthropic/claude-x"

    monkeypatch.setattr(mc, "fetch_models", _fake_fetch)
    monkeypatch.setattr(_inq, "fuzzy", lambda **k: _Fuzzy())
    cfg = Config()
    monkeypatch.setattr(loader, "load_config", lambda: cfg)
    saved = {}
    monkeypatch.setattr(loader, "save_config", lambda c: saved.update(model=c.agents.defaults.model))

    ob._prompt_model("anthropic")
    assert saved["model"] == "anthropic/claude-x"
    assert cfg.agents.defaults.model == "anthropic/claude-x"


def test_prompt_model_empty_catalog_keeps_default(monkeypatch):
    import flowly.config.loader as loader
    import flowly.integrations.model_catalog as mc

    async def _empty(slug, **k):
        return []

    monkeypatch.setattr(mc, "fetch_models", _empty)
    saved = {}
    monkeypatch.setattr(loader, "save_config", lambda c: saved.update(x=1))
    ob._prompt_model("nope")
    assert saved == {}  # no catalog → nothing written, default kept


def test_byok_providers_list_is_complete():
    slugs = {s for s, _, _ in ob._BYOK_PROVIDERS}
    # every key-based provider with a config slot is offered inline
    assert {"openrouter", "anthropic", "openai", "gemini", "groq", "xai", "zhipu", "sakana"} <= slugs


# ── section navigation (loops back to home) ──────────────────────────────

def test_sections_loop_back_then_quit_shows_summary(monkeypatch):
    calls = []
    seq = iter(["provider", "summary", "quit"])
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: next(seq))
    monkeypatch.setattr(ob, "_run_provider_step", lambda: calls.append("provider") or True)
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    ob._run_setup_home()
    # provider section loops back; explicit summary + quit-branch recap = two
    assert calls == ["provider", "summary", "summary"]


def test_quit_without_provider_prints_hint_not_summary(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "quit")
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    ob._run_setup_home()
    assert calls == []  # nothing configured → hint, no summary panel


# ── run_onboarding guards (Nuitka/Desktop safety) ────────────────────────

def test_non_tty_never_opens_home(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "seed_workspace", lambda: Path("/tmp/x"))
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    monkeypatch.setattr(ob.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(ob, "_run_setup_home", lambda: calls.append("home"))
    ob.run_onboarding()
    assert calls == []  # the isatty guard short-circuits


def test_already_configured_skips_home(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "seed_workspace", lambda: Path("/tmp/x"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    monkeypatch.setattr(ob, "_run_setup_home", lambda: calls.append("home"))
    ob.run_onboarding()
    assert calls == []


def test_tty_unconfigured_opens_home(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "seed_workspace", lambda: Path("/tmp/x"))
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    monkeypatch.setattr(ob.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ob, "_print_banner", lambda: None)
    monkeypatch.setattr(ob, "_run_setup_home", lambda: calls.append("home"))
    ob.run_onboarding()
    assert calls == ["home"]
