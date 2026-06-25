"""Tests for inline integration-card configuration (flowly/cli/inline_cards.py).

The InquirerPy prompts + config_io disk writes are mocked, so these assert the
value assembly (auto-enable, blank-means-skip) and the picker loop without any
terminal or disk I/O.
"""

from __future__ import annotations

import io

from InquirerPy import inquirer as _inquirer
from rich.console import Console

import flowly.cli.inline_cards as ic
import flowly.integrations.config_io as cio
from flowly.integrations.registry import get_card


def _console():
    return Console(file=io.StringIO(), force_terminal=False)


def test_channel_with_token_is_saved_and_auto_enabled(monkeypatch):
    card = get_card("telegram")  # enabled(bool) + token(required password) + …
    saved = {}
    monkeypatch.setattr(
        cio, "read_card_values",
        lambda c: {"enabled": False, "token": "", "allow_from": [], "dm_policy": "pairing"},
    )
    monkeypatch.setattr(cio, "apply_card_values", lambda c, v: saved.update(v))
    answers = {"token": "123:abc", "allow_from": [], "dm_policy": "pairing"}
    monkeypatch.setattr(ic, "_prompt_field", lambda f, cur: answers[f.key])

    assert ic.configure_card_inline(card, _console()) is True
    assert saved["token"] == "123:abc"
    assert saved["enabled"] is True  # a required secret was entered → auto-enabled


def test_channel_left_blank_is_not_saved(monkeypatch):
    card = get_card("telegram")
    saved = {}
    monkeypatch.setattr(
        cio, "read_card_values",
        lambda c: {"enabled": False, "token": "", "allow_from": [], "dm_policy": "pairing"},
    )
    monkeypatch.setattr(cio, "apply_card_values", lambda c, v: saved.update(v))
    answers = {"token": "", "allow_from": [], "dm_policy": "pairing"}
    monkeypatch.setattr(ic, "_prompt_field", lambda f, cur: answers[f.key])

    assert ic.configure_card_inline(card, _console()) is False
    assert saved == {}  # nothing entered → nothing written


def test_tool_saves_credentials_no_enabled_field(monkeypatch):
    card = get_card("linear")  # api_key (required password), no enabled toggle
    saved = {}
    monkeypatch.setattr(cio, "read_card_values", lambda c: {"api_key": ""})
    monkeypatch.setattr(cio, "apply_card_values", lambda c, v: saved.update(v))
    monkeypatch.setattr(ic, "_prompt_field", lambda f, cur: "lin_xxx")

    assert ic.configure_card_inline(card, _console()) is True
    assert saved["api_key"] == "lin_xxx"
    assert "enabled" not in saved  # linear has no enabled field


def test_section_picker_configures_then_done(monkeypatch):
    picks = iter(["linear", None])  # configure linear, then "← Done"

    class _Prompt:
        def execute(self):
            return next(picks)

    monkeypatch.setattr(_inquirer, "select", lambda **k: _Prompt())
    monkeypatch.setattr(ic, "_is_configured", lambda c: False)
    configured = []
    monkeypatch.setattr(ic, "configure_card_inline", lambda card, console: configured.append(card.key))

    ic.configure_section_inline("tool", "Tools", _console())
    assert configured == ["linear"]
