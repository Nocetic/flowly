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


class _FakeTranscript:
    def __init__(self) -> None:
        self.systems: list[str] = []
        self.errors: list[str] = []

    def add_system(self, m: str) -> None:
        self.systems.append(m)

    def add_error(self, m: str) -> None:
        self.errors.append(m)


class _FakeApp:
    def __init__(self, client, transcript) -> None:
        self._client = client
        self._transcript = transcript

    def query_one(self, _cls):
        return self._transcript


@pytest.mark.asyncio
async def test_cycle_applies_next_level_to_both_exec_and_codex():
    # Current live exec policy is full/off (matches YOLO), so the first press
    # advances to the next level → Ask.
    client = _FakeClient({"security": "full", "ask": "off"})
    transcript = _FakeTranscript()
    app = _FakeApp(client, transcript)

    await FlowlyTUI.action_cycle_permission(app)

    assert client.calls == [
        ("exec", "full", "always"),
        ("codex", "auto-review", "workspace-write"),
    ]
    assert transcript.systems and "🔒 Ask" in transcript.systems[0]
    assert not transcript.errors


@pytest.mark.asyncio
async def test_cycle_wraps_through_all_three_levels():
    client = _FakeClient({"security": "full", "ask": "off"})  # YOLO → next = Ask
    app = _FakeApp(client, _FakeTranscript())

    labels = []
    for _ in range(4):  # Ask, Auto, YOLO, then wrap back to Ask
        await FlowlyTUI.action_cycle_permission(app)
        labels.append(app._transcript.systems[-1].split("·")[0].strip())

    assert labels[0].endswith("Ask")
    assert labels[1].endswith("Auto")
    assert labels[2].endswith("YOLO")
    assert labels[3].endswith("Ask")  # wrapped


@pytest.mark.asyncio
async def test_cycle_surfaces_rpc_failure_without_crashing():
    class _Boom(_FakeClient):
        async def exec_policy_set(self, *, security=None, ask=None):
            raise RuntimeError("gateway down")

    app = _FakeApp(_Boom({"security": "full", "ask": "off"}), _FakeTranscript())
    await FlowlyTUI.action_cycle_permission(app)
    assert app._transcript.errors and "gateway down" in app._transcript.errors[0]
