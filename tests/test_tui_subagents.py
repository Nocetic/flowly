from __future__ import annotations

from flowly.tui.panes.subagents import SubagentPane, SubagentRow


class _FakeTimer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_subagent_toggle_manual_open_pins_and_close_unpins() -> None:
    pane = SubagentPane()

    pane.toggle()

    assert pane.visible is True
    assert pane._pinned is True

    pane.toggle()

    assert pane.visible is False
    assert pane._pinned is False


def test_subagent_auto_show_is_not_pinned_and_cancels_hide_timer(monkeypatch) -> None:
    pane = SubagentPane()
    pending = _FakeTimer()
    mounted = []
    pane._hide_timer = pending

    monkeypatch.setattr(pane, "mount", mounted.append)

    pane.add_started(
        {
            "runId": "run-1",
            "label": "research",
            "task": "look into it",
            "model": "step-3.7-flash",
        }
    )

    assert pending.stopped is True
    assert pane._hide_timer is None
    assert pane.visible is True
    assert pane._pinned is False
    assert isinstance(mounted[0], SubagentRow)


def test_subagent_completion_schedules_auto_hide_when_idle(monkeypatch) -> None:
    pane = SubagentPane()
    timers = []
    completed = []

    class _Row:
        def complete(self, status: str, error: str | None = None) -> None:
            completed.append((status, error))

    monkeypatch.setattr(pane, "_find", lambda run_id: _Row())
    monkeypatch.setattr(pane, "running_count", lambda: 0)
    monkeypatch.setattr(
        pane,
        "set_timer",
        lambda delay, callback: timers.append((delay, callback)) or _FakeTimer(),
    )

    pane.mark_completed({"runId": "run-1", "status": "ok", "error": None})

    assert completed == [("ok", None)]
    assert timers[0][0] == pane.AUTO_HIDE_DELAY


def test_subagent_auto_hide_respects_manual_pin(monkeypatch) -> None:
    pane = SubagentPane()
    timers = []
    pane.show(pinned=True)

    monkeypatch.setattr(pane, "running_count", lambda: 0)
    monkeypatch.setattr(
        pane,
        "set_timer",
        lambda delay, callback: timers.append((delay, callback)) or _FakeTimer(),
    )

    pane._schedule_auto_hide_if_idle()

    assert timers == []
    assert pane.visible is True


def test_subagent_auto_hide_only_closes_when_still_idle(monkeypatch) -> None:
    pane = SubagentPane()
    pane.show(pinned=False)

    monkeypatch.setattr(pane, "running_count", lambda: 1)
    pane._auto_hide_if_idle()

    assert pane.visible is True

    monkeypatch.setattr(pane, "running_count", lambda: 0)
    pane._auto_hide_if_idle()

    assert pane.visible is False
