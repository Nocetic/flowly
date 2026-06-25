"""Pure logic of the TUI policy editor modal, plus a mount smoke test."""

from __future__ import annotations

import pytest

from flowly.tui.panes.policy_modal import (
    ASK_CHOICES,
    SECURITY_CHOICES,
    PolicyModal,
    action_for_button,
)


def test_security_button_maps_to_set_action():
    assert action_for_button("sec-deny") == {"action": "set", "security": "deny"}
    assert action_for_button("sec-allowlist") == {"action": "set", "security": "allowlist"}
    assert action_for_button("sec-full") == {"action": "set", "security": "full"}


def test_ask_button_maps_to_set_action_including_dashed_value():
    assert action_for_button("ask-off") == {"action": "set", "ask": "off"}
    # The value itself contains a dash — parsing must keep it intact.
    assert action_for_button("ask-on-miss") == {"action": "set", "ask": "on-miss"}
    assert action_for_button("ask-always") == {"action": "set", "ask": "always"}


def test_non_choice_buttons_return_none():
    assert action_for_button(None) is None
    assert action_for_button("remove") is None
    assert action_for_button("close") is None


def test_choices_cover_backend_enums():
    assert [v for v, _ in SECURITY_CHOICES] == ["deny", "allowlist", "full"]
    assert [v for v, _ in ASK_CHOICES] == ["off", "on-miss", "always"]


def test_modal_reads_current_policy():
    modal = PolicyModal(
        {
            "security": "allowlist",
            "ask": "on-miss",
            "allowlist": [{"pattern": "/usr/bin/git", "command": "git *"}],
        }
    )
    assert modal._security == "allowlist"
    assert modal._ask == "on-miss"
    assert modal._allowlist[0]["pattern"] == "/usr/bin/git"


def test_modal_defaults_when_policy_empty():
    modal = PolicyModal({})
    assert modal._security == "full"
    assert modal._ask == "off"
    assert modal._allowlist == []


@pytest.mark.asyncio
async def test_modal_mounts_and_renders_controls():
    """Mount the modal in a throwaway app to catch compose/CSS errors and
    confirm the security/ask buttons exist with the expected ids."""
    from textual.app import App
    from textual.widgets import Button

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(
                PolicyModal(
                    {
                        "security": "allowlist",
                        "ask": "on-miss",
                        "allowlist": [{"pattern": "/usr/bin/git", "command": "git *"}],
                    }
                )
            )

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = {b.id for b in app.screen.query(Button)}
        for value, _ in SECURITY_CHOICES:
            assert f"sec-{value}" in ids
        for value, _ in ASK_CHOICES:
            assert f"ask-{value}" in ids


@pytest.mark.asyncio
async def test_selecting_applies_live_and_keeps_modal_open():
    """Clicking a choice applies via the callback and the modal STAYS OPEN
    (regression: it used to dismiss + reopen, looking like it never closed)."""
    from textual.app import App
    from textual.widgets import Button

    applied: list[dict] = []

    async def apply(action):
        applied.append(action)
        pol = {"security": "full", "ask": "off", "allowlist": []}
        if action.get("action") == "set":
            for k in ("security", "ask"):
                if action.get(k):
                    pol[k] = action[k]
        return pol

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(PolicyModal({"security": "full", "ask": "off", "allowlist": []}, apply))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#sec-deny")
        await pilot.pause()
        assert applied == [{"action": "set", "security": "deny"}]
        # Modal is still on screen, and the button reflects the new selection.
        assert isinstance(app.screen, PolicyModal)
        assert app.screen.query_one("#sec-deny", Button).variant == "success"


@pytest.mark.asyncio
async def test_close_button_dismisses_modal():
    from textual.app import App
    from textual.widgets import Button

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(PolicyModal({"security": "full", "ask": "off", "allowlist": []}))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, PolicyModal)
        # .press() posts Button.Pressed exactly like a real click; we use it
        # instead of pilot.click() which can't reliably hit a button at the
        # bottom of a 1fr layout in the headless harness.
        app.screen.query_one("#close-modal", Button).press()
        await pilot.pause()
        assert not isinstance(app.screen, PolicyModal)


@pytest.mark.asyncio
async def test_escape_dismisses_modal():
    from textual.app import App

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(PolicyModal({"security": "full", "ask": "off", "allowlist": []}))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PolicyModal)
