"""Tests for the setup-home routing + modes (flowly/cli/onboard_cmd.py).

The interactive menu + section modals are mocked, so these assert the
orchestration (which mode runs which steps, and that the non-TTY guard still
short-circuits before any prompt) without touching InquirerPy/Textual.
"""

from __future__ import annotations

import io
import types
from pathlib import Path

from rich.console import Console

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
    monkeypatch.setattr(
        ob, "_run_provider_step", lambda **kw: calls.append("provider") or True
    )
    monkeypatch.setattr(ob, "_verify_provider", lambda **kw: calls.append("verify") or True)
    monkeypatch.setattr(ob, "_configure_channels", lambda: calls.append("channels"))
    monkeypatch.setattr(ob, "_configure_tools", lambda: calls.append("tools"))
    monkeypatch.setattr(ob, "_configure_media", lambda: calls.append("media"))
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    assert calls == ["provider", "verify", "summary", "gateway"]  # no channels/tools/media


def test_blank_mode_does_not_ask_for_a_model(monkeypatch):
    """Model choice belongs to Full. A first-run user hasn't sent a message
    yet, so "which model?" is a question they cannot answer; the provider's
    curated default carries them, and /model is one keystroke later."""
    seen = {}
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "blank")
    monkeypatch.setattr(
        ob, "_run_provider_step", lambda **kw: seen.update(kw) or True
    )
    monkeypatch.setattr(ob, "_verify_provider", lambda **kw: True)
    monkeypatch.setattr(ob, "_show_summary", lambda: None)
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: None)
    ob._run_setup_home()
    assert seen == {"ask_model": False}


def test_full_mode_configures_channels_tools_media(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "full")
    monkeypatch.setattr(
        ob, "_run_provider_step", lambda **kw: calls.append("provider") or True
    )
    monkeypatch.setattr(ob, "_verify_provider", lambda **kw: True)
    monkeypatch.setattr(ob, "_configure_channels", lambda: calls.append("channels"))
    monkeypatch.setattr(ob, "_configure_tools", lambda: calls.append("tools"))
    monkeypatch.setattr(ob, "_configure_media", lambda: calls.append("media"))
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    assert calls == ["provider", "channels", "tools", "media", "summary", "gateway"]


def test_quick_mode_signs_in_without_showing_the_provider_list(monkeypatch):
    """Quick asks ONE question, and it isn't "which of these twelve?".

    The account sign-in needs no key, no billing setup and no model decision,
    so Quick commits to it; anyone who wants their own key picks Full. The
    picker must not appear here — that was the step a first-run user had no
    basis to answer.
    """
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "quick")
    monkeypatch.setattr(
        ob,
        "_run_provider_step",
        lambda **kw: calls.append("picker") or True,  # must NOT be reached
    )
    monkeypatch.setattr(ob, "_run_managed_login", lambda: calls.append("sign-in"))
    monkeypatch.setattr(ob, "_already_configured", lambda: True)
    monkeypatch.setattr(ob, "_verify_provider", lambda **kw: calls.append("verify") or True)
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))
    ob._run_setup_home()
    assert calls == ["sign-in", "verify", "summary", "gateway"]
    assert "picker" not in calls


def test_quick_mode_returns_home_when_sign_in_leaves_nothing_usable(monkeypatch):
    """Sign-in can succeed while the credential behind it never arrives."""
    calls = []
    monkeypatch.setattr(ob, "_setup_home_menu", lambda: "quick")
    monkeypatch.setattr(ob, "_run_managed_login", lambda: calls.append("sign-in"))
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    monkeypatch.setattr(ob, "_warn_signed_in_but_unusable", lambda: calls.append("warned"))
    monkeypatch.setattr(ob, "_verify_provider", lambda **kw: calls.append("verify") or True)
    monkeypatch.setattr(ob, "_show_summary", lambda: calls.append("summary"))
    monkeypatch.setattr(ob, "_offer_start_gateway", lambda: calls.append("gateway"))

    # The home would loop forever on a failed mode; stop after the first pass.
    seen = {"n": 0}

    def _menu():
        seen["n"] += 1
        return "quick" if seen["n"] == 1 else "quit"

    monkeypatch.setattr(ob, "_setup_home_menu", _menu)
    ob._run_setup_home()
    assert calls == ["sign-in", "warned"]  # no verify/summary/gateway on failure


def test_gateway_offer_reports_success_only_for_zero_exit(monkeypatch):
    from flowly.cli import service_cmd

    stream = io.StringIO()
    monkeypatch.setattr(
        ob,
        "console",
        Console(file=stream, force_terminal=False, color_system=None),
    )
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(service_cmd, "_resolve_flowly_exec_argv", lambda: ["/opt/flowly"])
    monkeypatch.setattr(
        ob.subprocess,
        "run",
        lambda argv, check=False: types.SimpleNamespace(returncode=0),
    )

    ob._offer_start_gateway()

    assert "✓ Done" in stream.getvalue()


def test_gateway_offer_does_not_claim_done_for_nonzero_exit(monkeypatch):
    from flowly.cli import service_cmd

    stream = io.StringIO()
    monkeypatch.setattr(
        ob,
        "console",
        Console(file=stream, force_terminal=False, color_system=None),
    )
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(service_cmd, "_resolve_flowly_exec_argv", lambda: ["/opt/flowly"])
    monkeypatch.setattr(
        ob.subprocess,
        "run",
        lambda argv, check=False: types.SimpleNamespace(returncode=1),
    )

    ob._offer_start_gateway()

    text = stream.getvalue()
    assert "Couldn't auto-start (exit 1)" in text
    assert "✓ Done" not in text
    assert "flowly service install --start" in text


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
    monkeypatch.setattr(ob, "_run_provider_step", lambda **kw: False)  # user backed out
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


# ── /dev/tty event-loop guard (installer runs `flowly setup </dev/tty`) ───

def test_tty_friendly_loop_uses_select_on_macos(monkeypatch):
    """On macOS the picker must run on a select()-backed loop: kqueue can't
    register /dev/tty, which is exactly what the installer hands us as stdin."""
    import asyncio
    import selectors

    monkeypatch.setattr(ob.sys, "platform", "darwin")
    prev = asyncio.get_event_loop_policy()
    with ob._tty_friendly_event_loop():
        loop = asyncio.new_event_loop()
        try:
            assert isinstance(loop._selector, selectors.SelectSelector)
        finally:
            loop.close()
    # policy restored on exit — nothing else in the process inherits select
    assert asyncio.get_event_loop_policy() is prev


def test_tty_friendly_loop_is_noop_off_macos(monkeypatch):
    import asyncio

    monkeypatch.setattr(ob.sys, "platform", "linux")
    prev = asyncio.get_event_loop_policy()
    with ob._tty_friendly_event_loop():
        assert asyncio.get_event_loop_policy() is prev  # untouched inside
    assert asyncio.get_event_loop_policy() is prev


def test_interactive_setup_swallows_event_loop_oserror(monkeypatch, capsys):
    """A terminal that still can't attach to an event loop must degrade to an
    actionable hint, never a raw traceback on a first-run user."""
    monkeypatch.setattr(ob, "seed_workspace", lambda: Path("/tmp/x"))
    monkeypatch.setattr(ob, "_already_configured", lambda: False)
    monkeypatch.setattr(ob.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ob, "_print_banner", lambda: None)

    def _boom():
        raise OSError("kqueue: /dev/tty is not registered")

    monkeypatch.setattr(ob, "_run_setup_home", _boom)
    ob.run_onboarding()  # must not raise
    assert "flowly setup" in capsys.readouterr().out
