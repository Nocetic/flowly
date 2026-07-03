"""Sentry tool: project fallback, stacktrace parsing, dispatch, approval gating."""

from __future__ import annotations

import pytest

from flowly.agent.tools.sentry import SentryTool, _top_frames


def _stub_request(tool, responses):
    calls = []

    async def fake(method, path, *, json=None, params=None):
        calls.append((method, path, json, params))
        return responses.get((method, path))

    tool._request = fake  # type: ignore[assignment]
    return calls


def test_project_fallback():
    tool = SentryTool(token="t", org="o", default_project="dp")
    assert tool._project("explicit") == "explicit"
    assert tool._project("") == "dp"


def test_project_missing_raises():
    tool = SentryTool(token="t", org="o", default_project="")
    with pytest.raises(RuntimeError, match="No project"):
        tool._project("")


def test_top_frames_extracts_innermost():
    event = {
        "entries": [
            {"type": "breadcrumbs", "data": {}},
            {"type": "exception", "data": {"values": [{
                "type": "ValueError",
                "value": "bad input",
                "stacktrace": {"frames": [
                    {"filename": "app/main.py", "function": "run", "lineNo": 10},
                    {"filename": "app/core.py", "function": "step", "lineNo": 42},
                ]},
            }]}},
        ]
    }
    frames = _top_frames(event)
    assert frames[0] == "ValueError: bad input"
    assert any("app/core.py:42 in step" in f for f in frames)


def test_top_frames_empty_on_garbage():
    assert _top_frames(None) == []
    assert _top_frames({"entries": []}) == []


@pytest.mark.asyncio
async def test_list_issues_formats(monkeypatch):
    tool = SentryTool(token="t", org="o", default_project="proj")
    _stub_request(tool, {
        ("GET", "/projects/o/proj/issues/"): [
            {"shortId": "PROJ-1", "title": "Boom", "count": "12", "userCount": 3, "id": "999"},
        ],
    })
    out = await tool.execute("list_issues")
    assert "PROJ-1" in out and "Boom" in out and "id=999" in out


@pytest.mark.asyncio
async def test_resolve_issue_denied_makes_no_write():
    tool = SentryTool(token="t", org="o")
    calls = _stub_request(tool, {})

    async def deny(desc, session_key=""):
        return False
    tool._require_approval = deny  # type: ignore[assignment]

    out = await tool.execute("resolve_issue", issue_id="999")
    assert "Cancelled" in out
    assert calls == []


@pytest.mark.asyncio
async def test_resolve_issue_approved_puts_status():
    tool = SentryTool(token="t", org="o")
    calls = _stub_request(tool, {
        ("PUT", "/issues/999/"): {"shortId": "PROJ-1", "status": "resolved"},
    })

    async def allow(desc, session_key=""):
        return True
    tool._require_approval = allow  # type: ignore[assignment]

    out = await tool.execute("resolve_issue", issue_id="999")
    assert "resolved" in out
    method, path, json, _ = calls[0]
    assert (method, path) == ("PUT", "/issues/999/")
    assert json == {"status": "resolved"}
