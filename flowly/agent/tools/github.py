"""GitHub tool — issues, pull requests, and comments via the REST API.

Uses a personal access token from config (integrations.github.token).
Read actions run freely; anything that writes to GitHub (create issue,
comment, close/reopen) requires user approval.

Repository resolution: most actions take an optional ``repo`` ("owner/name").
When omitted, the tool falls back to the ``origin`` remote of the current
runtime working directory (the open project), so inside a repo the agent can
say "list the open issues" without spelling out owner/name. An explicit
``repo`` always wins. A configured ``integrations.github.default_repo`` is the
final fallback.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool

_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"


def _parse_owner_repo(url: str) -> str | None:
    """Extract ``owner/name`` from an https or ssh GitHub remote URL."""
    url = url.strip()
    # git@github.com:owner/name.git  |  ssh://git@github.com/owner/name.git
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def _repo_from_runtime_cwd(session_key: str = "") -> str | None:
    """Best-effort ``owner/name`` from the project's origin remote."""
    import subprocess

    try:
        from flowly.runtime_cwd import resolve_runtime_cwd

        cwd = resolve_runtime_cwd(session_key=session_key or None)
    except Exception:
        return None
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0:
            return _parse_owner_repo(out.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class GitHubTool(Tool):
    """Manage GitHub issues, pull requests, and comments."""

    def __init__(self, token: str, default_repo: str = ""):
        self._token = token
        self._default_repo = (default_repo or "").strip()

    @property
    def name(self) -> str:
        return "github"

    @property
    def description(self) -> str:
        return (
            "Interact with GitHub. Actions: list_issues, get_issue, "
            "list_pull_requests, get_pull_request, get_pull_request_files, "
            "create_issue, add_comment, close_issue, reopen_issue. "
            "The 'repo' argument ('owner/name') is optional inside a git "
            "repository — it defaults to the origin remote of the open project. "
            "Write actions (create/comment/close/reopen) ask for approval."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_issues", "get_issue",
                        "list_pull_requests", "get_pull_request",
                        "get_pull_request_files",
                        "create_issue", "add_comment",
                        "close_issue", "reopen_issue",
                    ],
                    "description": "Action to perform.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository as 'owner/name'. Optional inside a git repo (defaults to the origin remote).",
                },
                "number": {
                    "type": "integer",
                    "description": "Issue or pull-request number (for get_*, add_comment, close/reopen).",
                },
                "title": {
                    "type": "string",
                    "description": "Issue title (for create_issue).",
                },
                "body": {
                    "type": "string",
                    "description": "Issue/comment body in markdown (for create_issue, add_comment).",
                },
                "labels": {
                    "type": "string",
                    "description": "Comma-separated label names (for create_issue).",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "State filter for list_issues / list_pull_requests (default open).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max items to return (default 20, max 100).",
                },
            },
            "required": ["action"],
        }

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "flowly-github-tool",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method, f"{_API}{path}", headers=headers,
                json=json, params=params, timeout=20,
            )
        if resp.status_code == 401:
            raise RuntimeError("GitHub token rejected (401). Check integrations.github.token.")
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise RuntimeError("GitHub rate limit hit (403). Try again later.")
        if resp.status_code == 404:
            raise RuntimeError("Not found (404) — check the repo ('owner/name') and number.")
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 204:
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # Repo resolution
    # ------------------------------------------------------------------

    def _resolve_repo(self, explicit: str, session_key: str) -> str:
        repo = (explicit or "").strip()
        if not repo:
            repo = _repo_from_runtime_cwd(session_key) or self._default_repo
        if not repo or "/" not in repo:
            raise RuntimeError(
                "No repository. Pass repo='owner/name', or run inside a git "
                "repo with a GitHub origin remote, or set "
                "integrations.github.default_repo."
            )
        return repo

    # ------------------------------------------------------------------
    # Approval (write actions)
    # ------------------------------------------------------------------

    async def _require_approval(self, description: str, session_key: str = "") -> bool:
        import secrets

        from flowly.exec.approval_manager import get_approval_manager
        from flowly.exec.types import ExecRequest, PendingApproval

        pending = PendingApproval(
            id=secrets.token_hex(8),
            request=ExecRequest(command=description),
            created_at=time.time(),
            expires_at=time.time() + 120,
            session_key=session_key,
            supports_always=False,
        )
        try:
            decision = await get_approval_manager().request_and_wait(pending)
            return decision not in (None, "deny")
        except Exception as e:
            logger.error(f"[GitHub] Approval error: {e}")
            return False

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def execute(self, action: str, **kwargs: Any) -> str:
        session_key = kwargs.get("session_key", "")
        try:
            repo = self._resolve_repo(kwargs.get("repo", ""), session_key)
            if action == "list_issues":
                return await self._list_issues(repo, kwargs)
            if action == "get_issue":
                return await self._get_issue(repo, kwargs)
            if action == "list_pull_requests":
                return await self._list_prs(repo, kwargs)
            if action == "get_pull_request":
                return await self._get_pr(repo, kwargs)
            if action == "get_pull_request_files":
                return await self._get_pr_files(repo, kwargs)
            if action == "create_issue":
                return await self._create_issue(repo, kwargs, session_key)
            if action == "add_comment":
                return await self._add_comment(repo, kwargs, session_key)
            if action in ("close_issue", "reopen_issue"):
                return await self._set_issue_state(repo, kwargs, session_key, action)
            return f"Unknown action: {action}"
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("[GitHub] unexpected error")
            return f"Error: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Read actions
    # ------------------------------------------------------------------

    @staticmethod
    def _cap(kwargs: dict) -> int:
        try:
            n = int(kwargs.get("max_results", 20))
        except (TypeError, ValueError):
            n = 20
        return max(1, min(n, 100))

    async def _list_issues(self, repo: str, kwargs: dict) -> str:
        state = kwargs.get("state", "open")
        data = await self._request(
            "GET", f"/repos/{repo}/issues",
            params={"state": state, "per_page": self._cap(kwargs)},
        )
        # /issues returns PRs too; filter them out.
        issues = [i for i in (data or []) if "pull_request" not in i]
        if not issues:
            return f"No {state} issues in {repo}."
        lines = [f"{state} issues in {repo}:"]
        for i in issues:
            labels = ", ".join(lbl["name"] for lbl in i.get("labels", []))
            suffix = f"  [{labels}]" if labels else ""
            lines.append(f"  #{i['number']} {i['title']}{suffix}")
        return "\n".join(lines)

    async def _get_issue(self, repo: str, kwargs: dict) -> str:
        num = kwargs.get("number")
        issue = await self._request("GET", f"/repos/{repo}/issues/{num}")
        comments = await self._request(
            "GET", f"/repos/{repo}/issues/{num}/comments", params={"per_page": 20}
        )
        out = [
            f"#{issue['number']} {issue['title']} ({issue['state']})",
            f"by {issue['user']['login']} — {issue.get('html_url', '')}",
            "",
            (issue.get("body") or "(no description)").strip(),
        ]
        if comments:
            out.append("\n--- comments ---")
            for c in comments:
                out.append(f"{c['user']['login']}: {(c.get('body') or '').strip()}")
        return "\n".join(out)

    async def _list_prs(self, repo: str, kwargs: dict) -> str:
        state = kwargs.get("state", "open")
        prs = await self._request(
            "GET", f"/repos/{repo}/pulls",
            params={"state": state, "per_page": self._cap(kwargs)},
        )
        if not prs:
            return f"No {state} pull requests in {repo}."
        lines = [f"{state} pull requests in {repo}:"]
        for p in prs:
            draft = " (draft)" if p.get("draft") else ""
            lines.append(f"  #{p['number']} {p['title']}{draft}  [{p['head']['ref']} → {p['base']['ref']}]")
        return "\n".join(lines)

    async def _get_pr(self, repo: str, kwargs: dict) -> str:
        num = kwargs.get("number")
        pr = await self._request("GET", f"/repos/{repo}/pulls/{num}")
        out = [
            f"#{pr['number']} {pr['title']} ({pr['state']}{', merged' if pr.get('merged') else ''})",
            f"{pr['head']['ref']} → {pr['base']['ref']} · +{pr.get('additions', '?')}/-{pr.get('deletions', '?')} in {pr.get('changed_files', '?')} files",
            f"by {pr['user']['login']} — {pr.get('html_url', '')}",
            "",
            (pr.get("body") or "(no description)").strip(),
        ]
        return "\n".join(out)

    async def _get_pr_files(self, repo: str, kwargs: dict) -> str:
        num = kwargs.get("number")
        files = await self._request(
            "GET", f"/repos/{repo}/pulls/{num}/files", params={"per_page": self._cap(kwargs)}
        )
        if not files:
            return f"PR #{num} has no files."
        lines = [f"Files in PR #{num}:"]
        for f in files:
            lines.append(f"  {f['status']:9} +{f['additions']}/-{f['deletions']}  {f['filename']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Write actions (approval-gated)
    # ------------------------------------------------------------------

    async def _create_issue(self, repo: str, kwargs: dict, session_key: str) -> str:
        title = (kwargs.get("title") or "").strip()
        if not title:
            return "Error: create_issue requires a title."
        if not await self._require_approval(f"Create GitHub issue in {repo}: {title}", session_key):
            return "Cancelled — issue not created."
        payload: dict[str, Any] = {"title": title, "body": kwargs.get("body", "")}
        labels = (kwargs.get("labels") or "").strip()
        if labels:
            payload["labels"] = [lbl.strip() for lbl in labels.split(",") if lbl.strip()]
        issue = await self._request("POST", f"/repos/{repo}/issues", json=payload)
        return f"Created issue #{issue['number']}: {issue.get('html_url', '')}"

    async def _add_comment(self, repo: str, kwargs: dict, session_key: str) -> str:
        num = kwargs.get("number")
        body = (kwargs.get("body") or "").strip()
        if not num or not body:
            return "Error: add_comment requires number and body."
        if not await self._require_approval(f"Comment on {repo}#{num}", session_key):
            return "Cancelled — comment not posted."
        c = await self._request(
            "POST", f"/repos/{repo}/issues/{num}/comments", json={"body": body}
        )
        return f"Comment posted: {c.get('html_url', '')}"

    async def _set_issue_state(self, repo: str, kwargs: dict, session_key: str, action: str) -> str:
        num = kwargs.get("number")
        if not num:
            return f"Error: {action} requires a number."
        new_state = "closed" if action == "close_issue" else "open"
        if not await self._require_approval(f"Set {repo}#{num} to {new_state}", session_key):
            return f"Cancelled — {repo}#{num} unchanged."
        issue = await self._request(
            "PATCH", f"/repos/{repo}/issues/{num}", json={"state": new_state}
        )
        return f"#{issue['number']} is now {issue['state']}."
