"""The TUI permission-cycle levels (Shift+Tab) must stay in lockstep with what
the exec.policy.set / codex.policy.set RPCs actually accept — a typo in a level
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
    # A custom/unrecognised policy → -1, so the first cycle lands on level 0.
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

    from flowly.tui.panes.status import _PERMISSION_BADGE, StatusBar, _PermissionBadge

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


@pytest.mark.asyncio
async def test_shift_tab_dispatches_cycle_in_the_real_app():
    # The key binding must actually fire in the real app: Shift+Tab is an
    # app-level binding (so Textual awaits the async action) and plain Tab is
    # left to the composer's autocomplete. Mount offline so no gateway/config
    # is needed.
    from flowly.tui.client import GatewayUnavailable

    class _OfflineClient:
        async def connect(self):
            raise GatewayUnavailable("no gateway (test)")

        async def close(self):
            pass

    class _Probe(FlowlyTUI):
        fired = 0

        async def action_cycle_permission(self):  # avoid real RPC
            _Probe.fired += 1

    app = _Probe(client=_OfflineClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert _Probe.fired == 1
        # Plain Tab must NOT cycle — it belongs to composer autocomplete.
        await pilot.press("tab")
        await pilot.pause()
        assert _Probe.fired == 1


# --- _sync_permission_badge: live poll picks up out-of-band changes ----------


@pytest.mark.asyncio
async def test_sync_reflects_a_mode_changed_elsewhere():
    # The poll re-reads the live exec policy, so a mode set from the Desktop app
    # (or another client) shows up on the badge without a restart.
    app = _FakeApp(_FakeClient({"security": "full", "ask": "always"}))  # Ask
    await FlowlyTUI._sync_permission_badge(app)
    assert app._status.permission == "ask"

    app._client._current = {"security": "full", "ask": "off"}  # → YOLO elsewhere
    await FlowlyTUI._sync_permission_badge(app)
    assert app._status.permission == "yolo"


@pytest.mark.asyncio
async def test_sync_hides_badge_when_policy_matches_no_preset():
    app = _FakeApp(_FakeClient({"security": "deny", "ask": "off"}))
    app._status.permission = "yolo"  # stale
    await FlowlyTUI._sync_permission_badge(app)
    assert app._status.permission == ""  # hidden (custom/unknown)


@pytest.mark.asyncio
async def test_sync_skips_while_a_manual_cycle_is_applying():
    app = _FakeApp(_FakeClient({"security": "full", "ask": "off"}))  # YOLO on disk
    app._status.permission = "ask"  # a manual cycle just set this optimistically
    app._perm_cycling = True
    await FlowlyTUI._sync_permission_badge(app)
    assert app._status.permission == "ask"  # poll skipped, no clobber
