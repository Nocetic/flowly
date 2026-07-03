"""GitHub tool: repo resolution, dispatch/formatting, and write-approval gating.

No network: the HTTP layer (``_request``) and the approval prompt are stubbed.
"""

from __future__ import annotations

import pytest

from flowly.agent.tools.github import GitHubTool, _parse_owner_repo


# ── URL parsing ────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:Nocetic/flowly.git", "Nocetic/flowly"),
    ("https://github.com/Nocetic/flowly", "Nocetic/flowly"),
    ("https://github.com/Nocetic/flowly.git", "Nocetic/flowly"),
    ("ssh://git@github.com/Nocetic/flowly.git", "Nocetic/flowly"),
    ("https://gitlab.com/x/y.git", None),
    ("not a url", None),
])
def test_parse_owner_repo(url, expected):
    assert _parse_owner_repo(url) == expected


# ── repo resolution priority ───────────────────────────────────────


def test_explicit_repo_wins(monkeypatch):
    tool = GitHubTool(token="t", default_repo="cfg/repo")
    monkeypatch.setattr("flowly.agent.tools.github._repo_from_runtime_cwd", lambda sk="": "git/repo")
    assert tool._resolve_repo("explicit/repo", "") == "explicit/repo"


def test_runtime_cwd_repo_used_when_no_explicit(monkeypatch):
    tool = GitHubTool(token="t", default_repo="cfg/repo")
    monkeypatch.setattr("flowly.agent.tools.github._repo_from_runtime_cwd", lambda sk="": "git/repo")
    assert tool._resolve_repo("", "") == "git/repo"


def test_default_repo_is_last_fallback(monkeypatch):
    tool = GitHubTool(token="t", default_repo="cfg/repo")
    monkeypatch.setattr("flowly.agent.tools.github._repo_from_runtime_cwd", lambda sk="": None)
    assert tool._resolve_repo("", "") == "cfg/repo"


def test_no_repo_raises(monkeypatch):
    tool = GitHubTool(token="t", default_repo="")
    monkeypatch.setattr("flowly.agent.tools.github._repo_from_runtime_cwd", lambda sk="": None)
    with pytest.raises(RuntimeError, match="No repository"):
        tool._resolve_repo("", "")


# ── read actions (stubbed HTTP) ────────────────────────────────────


def _stub_request(tool, responses):
    """Replace tool._request with a recorder returning canned responses.

    responses: dict keyed by (METHOD, path) → payload. Records calls.
    """
    calls = []

    async def fake(method, path, *, json=None, params=None):
        calls.append((method, path, json, params))
        return responses.get((method, path))

    tool._request = fake  # type: ignore[assignment]
    return calls


@pytest.mark.asyncio
async def test_list_issues_filters_out_prs(monkeypatch):
    tool = GitHubTool(token="t", default_repo="o/r")
    _stub_request(tool, {
        ("GET", "/repos/o/r/issues"): [
            {"number": 1, "title": "a bug", "labels": [{"name": "bug"}]},
            {"number": 2, "title": "a PR", "pull_request": {"url": "..."}},
        ],
    })
    out = await tool.execute("list_issues", repo="o/r")
    assert "#1 a bug" in out and "[bug]" in out
    assert "a PR" not in out  # PRs excluded from the issues list


@pytest.mark.asyncio
async def test_get_pull_request_files(monkeypatch):
    tool = GitHubTool(token="t", default_repo="o/r")
    _stub_request(tool, {
        ("GET", "/repos/o/r/pulls/5/files"): [
            {"status": "modified", "additions": 3, "deletions": 1, "filename": "a.py"},
        ],
    })
    out = await tool.execute("get_pull_request_files", repo="o/r", number=5)
    assert "a.py" in out and "+3/-1" in out


# ── write actions: approval gating ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_issue_denied_makes_no_write(monkeypatch):
    tool = GitHubTool(token="t", default_repo="o/r")
    calls = _stub_request(tool, {})

    async def deny(desc, session_key=""):
        return False
    tool._require_approval = deny  # type: ignore[assignment]

    out = await tool.execute("create_issue", repo="o/r", title="X")
    assert "Cancelled" in out
    assert calls == []  # nothing was POSTed


@pytest.mark.asyncio
async def test_create_issue_approved_posts(monkeypatch):
    tool = GitHubTool(token="t", default_repo="o/r")
    calls = _stub_request(tool, {
        ("POST", "/repos/o/r/issues"): {"number": 42, "html_url": "https://x/42"},
    })

    async def allow(desc, session_key=""):
        return True
    tool._require_approval = allow  # type: ignore[assignment]

    out = await tool.execute("create_issue", repo="o/r", title="X", labels="bug, ui")
    assert "#42" in out
    method, path, json, _ = calls[0]
    assert (method, path) == ("POST", "/repos/o/r/issues")
    assert json["labels"] == ["bug", "ui"]


@pytest.mark.asyncio
async def test_create_issue_requires_title():
    tool = GitHubTool(token="t", default_repo="o/r")
    out = await tool.execute("create_issue", repo="o/r")
    assert "requires a title" in out
