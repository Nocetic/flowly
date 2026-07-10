from __future__ import annotations

from flowly.tui import entry
from flowly.tui.state import canonical_session_key


def test_default_launch_starts_a_fresh_session(monkeypatch) -> None:
    monkeypatch.setattr(
        entry, "load_state", lambda: {"last_session_key": "cli:old-session"}
    )
    key = entry.resolve_startup_session("", False)
    assert key.startswith("cli:")
    assert key != "cli:old-session"


def test_fresh_sessions_are_unique(monkeypatch) -> None:
    monkeypatch.setattr(entry, "load_state", lambda: {})
    first = entry.resolve_startup_session("", False)
    second = entry.resolve_startup_session("", False)
    assert first != second


def test_resume_reopens_the_last_session(monkeypatch) -> None:
    monkeypatch.setattr(
        entry, "load_state", lambda: {"last_session_key": "cli:old-session"}
    )
    assert entry.resolve_startup_session("", True) == "cli:old-session"


def test_resume_canonicalises_legacy_key_and_persists(monkeypatch) -> None:
    saved: dict[str, object] = {}
    monkeypatch.setattr(
        entry, "load_state", lambda: {"last_session_key": "tui-20260528-043250"}
    )
    monkeypatch.setattr(entry, "save_state", lambda state: saved.update(state))

    key = entry.resolve_startup_session("", True)

    assert key == canonical_session_key("tui-20260528-043250")
    assert saved["last_session_key"] == key


def test_resume_without_state_falls_back_to_fresh(monkeypatch) -> None:
    monkeypatch.setattr(entry, "load_state", lambda: {})
    key = entry.resolve_startup_session("", True)
    assert key.startswith("cli:")


def test_explicit_session_wins_over_resume(monkeypatch) -> None:
    monkeypatch.setattr(
        entry, "load_state", lambda: {"last_session_key": "cli:old-session"}
    )
    key = entry.resolve_startup_session("tui-explicit", True)
    assert key == canonical_session_key("tui-explicit")


def test_persist_state_only_marks_sessions_that_were_used(monkeypatch) -> None:
    """An idle launch+quit must not steal --resume from the last real chat."""
    from flowly.tui import app as app_mod

    monkeypatch.setattr(
        app_mod, "load_state", lambda: {"last_session_key": "cli:previous"}
    )
    saved: dict[str, object] = {}
    monkeypatch.setattr(app_mod, "save_state", lambda state: saved.update(state))

    tui = app_mod.FlowlyTUI(client=None, session_key="cli:idle-launch")
    tui._persist_state()
    assert saved["last_session_key"] == "cli:previous"

    saved.clear()
    tui._session_used = True
    tui._persist_state()
    assert saved["last_session_key"] == "cli:idle-launch"


def test_resume_rows_label_content_and_limit() -> None:
    sessions = [
        {
            "key": "cli:recent",
            "title": "Artifact strip design",
            "channel": "cli",
            "modifiedAt": 0,
            "running": True,
        },
        {
            "key": "telegram:42",
            "title": "  spaced   out\ntitle ",
            "channel": "telegram",
            "modifiedAt": 0,
        },
        {"key": "", "title": "keyless — skipped"},
        {
            "key": "cli:long",
            "title": "x" * 90,
            "channel": "cli",
            "modifiedAt": 0,
        },
    ]

    rows = entry._resume_rows(sessions, limit=3)

    assert [key for key, _ in rows] == ["cli:recent", "telegram:42"]
    assert rows[0][1].startswith("● Artifact strip design")
    assert "ago" in rows[0][1]
    assert rows[1][1].startswith("[telegram] spaced out title")

    rows = entry._resume_rows(sessions)
    assert rows[2][1].startswith("x" * 56)
    assert "…" in rows[2][1]


def test_pick_resume_session_returns_canonical_choice(monkeypatch) -> None:
    from flowly.channels import feature_rpc

    monkeypatch.setattr(
        feature_rpc,
        "sessions_list",
        lambda: {"sessions": [{"key": "tui-legacy", "modifiedAt": 1}]},
    )
    monkeypatch.setattr(entry, "_prompt_pick", lambda rows: "tui-legacy")

    assert entry.pick_resume_session() == canonical_session_key("tui-legacy")


def test_pick_resume_session_none_when_user_backs_out(monkeypatch) -> None:
    from flowly.channels import feature_rpc

    monkeypatch.setattr(
        feature_rpc,
        "sessions_list",
        lambda: {"sessions": [{"key": "cli:one", "modifiedAt": 1}]},
    )
    monkeypatch.setattr(entry, "_prompt_pick", lambda rows: None)

    assert entry.pick_resume_session() is None


def test_pick_resume_session_fresh_when_no_sessions(monkeypatch) -> None:
    from flowly.channels import feature_rpc

    monkeypatch.setattr(feature_rpc, "sessions_list", lambda: {"sessions": []})

    key = entry.pick_resume_session()
    assert key is not None and key.startswith("cli:")
