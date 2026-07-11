"""Regression: a tool-less turn must never be action-enforced.

The flowlet vision runner disables EVERY tool and feeds an attacker-supplied
photo. If the photo's prompt happens to contain an action word — the expense
flowlet's prompt literally said "card statement screenshot", which hits the
``\\bscreenshot\\b`` pattern in ``_is_action_turn`` — the turn was flipped to
action-enforce mode. But with zero callable tools the enforcement loop can
never see a tool call: it retried twice, alarmed ("executed_tools=0"), then
fell back to a summary. The capture looked "broken" to the user.

``_all_tools_disabled`` detects the tool-less case so the caller drops the
action-turn flag. This test pins that logic.
"""

from __future__ import annotations

from flowly.agent.loop import AgentLoop


class _Tools:
    def __init__(self, names):
        self._names = list(names)

    @property
    def tool_names(self):
        return list(self._names)


def _agent(names) -> AgentLoop:
    agent = object.__new__(AgentLoop)  # bypass heavy __init__
    agent.tools = _Tools(names)
    return agent


def test_all_tools_disabled_when_every_name_is_disabled():
    agent = _agent(["message", "screenshot", "web_search"])
    assert agent._all_tools_disabled(["message", "screenshot", "web_search"]) is True


def test_not_disabled_when_one_tool_remains():
    agent = _agent(["message", "screenshot", "web_search"])
    assert agent._all_tools_disabled(["message", "screenshot"]) is False


def test_empty_disabled_list_is_not_all_disabled():
    agent = _agent(["message"])
    assert agent._all_tools_disabled([]) is False
    assert agent._all_tools_disabled(None) is False


def test_extra_disabled_names_still_count_as_all_disabled():
    # The vision runner passes list(tool_names); if the registry shrank between
    # enumerations, a superset of disabled names must still read as "all off".
    agent = _agent(["message", "screenshot"])
    assert agent._all_tools_disabled(["message", "screenshot", "gone"]) is True


def test_unreadable_registry_fails_safe_to_keep_enforcing():
    agent = object.__new__(AgentLoop)
    agent.tools = object()  # no .tool_names → AttributeError inside the guard
    assert agent._all_tools_disabled(["anything"]) is False


def test_empty_registry_is_not_all_disabled():
    # No tools registered at all is a degenerate config, not a vision turn —
    # don't silently clear enforcement for it.
    agent = _agent([])
    assert agent._all_tools_disabled(["x"]) is False
