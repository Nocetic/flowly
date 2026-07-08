"""The TUI F5 permission-cycle levels must stay in lockstep with what the
exec.policy.set / codex.policy.set RPCs actually accept — a typo in a level
would make the live apply fail at runtime, which a unit test should catch first.
"""

from __future__ import annotations

from flowly.channels import feature_rpc
from flowly.tui.app import _PERMISSION_LEVELS, _match_permission_level


def test_levels_use_only_values_the_rpcs_accept():
    for _key, _label, (security, ask), (approval, sandbox) in _PERMISSION_LEVELS:
        assert security in feature_rpc._EXEC_SECURITY, security
        assert ask in feature_rpc._EXEC_ASK, ask
        assert approval in feature_rpc._CODEX_APPROVAL, approval
        assert sandbox in feature_rpc._CODEX_SANDBOX, sandbox


def test_three_named_levels_in_order():
    assert [lvl[0] for lvl in _PERMISSION_LEVELS] == ["ask", "auto", "yolo"]


def test_match_permission_level_finds_current_by_exec_policy():
    assert _match_permission_level({"security": "full", "ask": "always"}) == 0
    assert _match_permission_level({"security": "allowlist", "ask": "on-miss"}) == 1
    assert _match_permission_level({"security": "full", "ask": "off"}) == 2


def test_match_permission_level_unknown_is_minus_one():
    # A custom/unrecognised policy → -1, so the first F5 press lands on level 0.
    assert _match_permission_level({"security": "deny", "ask": "off"}) == -1
    assert _match_permission_level({}) == -1


def test_status_badge_covers_every_level():
    # Every level must have a colored badge, or the status bar would go blank
    # when cycled to an unbadged level.
    from flowly.tui.panes.status import _PERMISSION_BADGE
    assert set(_PERMISSION_BADGE) == {lvl[0] for lvl in _PERMISSION_LEVELS}


# --- action_cycle_permission end-to-end (fake client, no Textual runtime) ----

import pytest  # noqa: E402

from flowly.tui.app import FlowlyTUI  # noqa: E402


class _FakeClient:
    def __init__(self, current: dict) -> None:
        self._current = current
        self.calls: list = []

    async def exec_policy_get(self) -> dict:
        return self._current

    async def exec_policy_set(self, *, security=None, ask=None) -> dict:
        self.calls.append(("exec", security, ask))
        return {"security": security, "ask": ask}

    async def codex_policy_set(self, *, approval_policy=None, sandbox=None) -> dict:
        self.calls.append(("codex", approval_policy, sandbox))
        return {"ok": True, "willRestart": False}


class _FakeStatus:
    def __init__(self) -> None:
        self.permission = ""


class _FakeTranscript:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def add_error(self, m: str) -> None:
        self.errors.append(m)


class _FakeApp:
    # Reuse the real badge setter so the test exercises the actual wiring.
    _set_permission_badge = FlowlyTUI._set_permission_badge

    def __init__(self, client) -> None:
        self._client = client
        self._status = _FakeStatus()
        self._transcript = _FakeTranscript()

    def query_one(self, cls):
        # The action asks for StatusBar (badge) and TranscriptPane (errors).
        if getattr(cls, "__name__", "") == "StatusBar":
            return self._status
        return self._transcript


@pytest.mark.asyncio
async def test_cycle_applies_next_level_and_updates_badge():
    # Current live exec policy is full/off (matches YOLO), so the first press
    # advances to the next level → Ask.
    app = _FakeApp(_FakeClient({"security": "full", "ask": "off"}))

    await FlowlyTUI.action_cycle_permission(app)

    assert app._client.calls == [
        ("exec", "full", "always"),
        ("codex", "auto-review", "workspace-write"),
    ]
    # The indicator lives on the status bar, NOT in the transcript.
    assert app._status.permission == "ask"
    assert not app._transcript.errors


@pytest.mark.asyncio
async def test_cycle_wraps_through_all_three_levels():
    app = _FakeApp(_FakeClient({"security": "full", "ask": "off"}))  # YOLO → Ask next

    seen = []
    for _ in range(4):  # ask, auto, yolo, then wrap back to ask
        await FlowlyTUI.action_cycle_permission(app)
        seen.append(app._status.permission)

    assert seen == ["ask", "auto", "yolo", "ask"]


@pytest.mark.asyncio
async def test_cycle_surfaces_rpc_failure_and_leaves_badge_unchanged():
    class _Boom(_FakeClient):
        async def exec_policy_set(self, *, security=None, ask=None):
            raise RuntimeError("gateway down")

    app = _FakeApp(_Boom({"security": "full", "ask": "off"}))
    await FlowlyTUI.action_cycle_permission(app)

    assert app._transcript.errors and "gateway down" in app._transcript.errors[0]
    assert app._status.permission == ""  # not advanced on failure


@pytest.mark.asyncio
async def test_status_bar_renders_colored_badge():
    # Mount the real StatusBar and confirm the leftmost badge shows/hides and
    # colors by level — the visual half of the feature, OS-independent.
    from textual.app import App, ComposeResult

    from flowly.tui.panes.status import StatusBar, _PermissionBadge, _PERMISSION_BADGE

    class _Harness(App):
        def compose(self) -> ComposeResult:
            yield StatusBar()

    app = _Harness()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        badge = bar.query_one(_PermissionBadge)
        assert badge.display is False  # hidden until a level is known
        for level in ("ask", "auto", "yolo"):
            bar.permission = level
            await pilot.pause()
            assert badge.display is True
            _color, label = _PERMISSION_BADGE[level]
            assert label.split()[-1] in str(badge.render())
        bar.permission = ""
        await pilot.pause()
        assert badge.display is False
