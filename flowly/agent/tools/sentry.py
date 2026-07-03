"""Sentry tool — read error issues and events via the REST API.

Uses an auth token + org slug from config (integrations.sentry.token,
integrations.sentry.org). Read actions run freely; changing an issue's
state (resolve / ignore) requires user approval.

Docs: https://docs.sentry.io/api/
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool

_API = "https://sentry.io/api/0"


class SentryTool(Tool):
    """Read Sentry projects, issues, and event details."""

    def __init__(self, token: str, org: str, default_project: str = ""):
        self._token = token
        self._org = (org or "").strip()
        self._default_project = (default_project or "").strip()

    @property
    def name(self) -> str:
        return "sentry"

    @property
    def description(self) -> str:
        return (
            "Read errors from Sentry. Actions: list_projects, list_issues "
            "(unresolved errors for a project), get_issue (details + latest "
            "event stacktrace), resolve_issue, ignore_issue. The 'project' "
            "argument defaults to the configured project. Changing issue "
            "state (resolve/ignore) asks for approval."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_projects", "list_issues", "get_issue",
                        "resolve_issue", "ignore_issue",
                    ],
                    "description": "Action to perform.",
                },
                "project": {
                    "type": "string",
                    "description": "Project slug (for list_issues). Defaults to the configured project.",
                },
                "issue_id": {
                    "type": "string",
                    "description": "Sentry issue ID (for get_issue, resolve_issue, ignore_issue).",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for list_issues (default 'is:unresolved').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max issues to return (default 20, max 100).",
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
            "Content-Type": "application/json",
            "User-Agent": "flowly-sentry-tool",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method, f"{_API}{path}", headers=headers,
                json=json, params=params, timeout=20,
            )
        if resp.status_code == 401:
            raise RuntimeError("Sentry token rejected (401). Check integrations.sentry.token.")
        if resp.status_code == 403:
            raise RuntimeError("Sentry token lacks permission (403) for this resource.")
        if resp.status_code == 404:
            raise RuntimeError("Not found (404) — check the org, project slug, or issue ID.")
        if resp.status_code >= 400:
            raise RuntimeError(f"Sentry API {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 204:
            return None
        return resp.json()

    def _project(self, explicit: str) -> str:
        project = (explicit or "").strip() or self._default_project
        if not project:
            raise RuntimeError(
                "No project. Pass project='slug' or set integrations.sentry.default_project."
            )
        return project

    # ------------------------------------------------------------------
    # Approval
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
            logger.error(f"[Sentry] Approval error: {e}")
            return False

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def execute(self, action: str, **kwargs: Any) -> str:
        session_key = kwargs.get("session_key", "")
        try:
            if action == "list_projects":
                return await self._list_projects()
            if action == "list_issues":
                return await self._list_issues(kwargs)
            if action == "get_issue":
                return await self._get_issue(kwargs)
            if action in ("resolve_issue", "ignore_issue"):
                return await self._set_state(kwargs, session_key, action)
            return f"Unknown action: {action}"
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("[Sentry] unexpected error")
            return f"Error: {type(e).__name__}: {e}"

    @staticmethod
    def _cap(kwargs: dict) -> int:
        try:
            n = int(kwargs.get("max_results", 20))
        except (TypeError, ValueError):
            n = 20
        return max(1, min(n, 100))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _list_projects(self) -> str:
        data = await self._request("GET", f"/organizations/{self._org}/projects/")
        if not data:
            return f"No projects in org {self._org}."
        lines = [f"Projects in {self._org}:"]
        for p in data:
            lines.append(f"  {p['slug']}  ({p.get('platform') or 'unknown'})")
        return "\n".join(lines)

    async def _list_issues(self, kwargs: dict) -> str:
        project = self._project(kwargs.get("project", ""))
        query = kwargs.get("query") or "is:unresolved"
        data = await self._request(
            "GET", f"/projects/{self._org}/{project}/issues/",
            params={"query": query, "limit": self._cap(kwargs)},
        )
        if not data:
            return f"No issues matching '{query}' in {project}."
        lines = [f"Issues in {project} ({query}):"]
        for i in data:
            lines.append(
                f"  [{i['shortId']}] {i['title']}  "
                f"(events: {i.get('count', '?')}, users: {i.get('userCount', '?')})  id={i['id']}"
            )
        return "\n".join(lines)

    async def _get_issue(self, kwargs: dict) -> str:
        issue_id = (kwargs.get("issue_id") or "").strip()
        if not issue_id:
            return "Error: get_issue requires issue_id."
        issue = await self._request("GET", f"/issues/{issue_id}/")
        out = [
            f"[{issue['shortId']}] {issue['title']} ({issue.get('status', '?')})",
            f"culprit: {issue.get('culprit', '?')}",
            f"events: {issue.get('count', '?')}, users: {issue.get('userCount', '?')}, "
            f"first: {issue.get('firstSeen', '?')}, last: {issue.get('lastSeen', '?')}",
            f"{issue.get('permalink', '')}",
        ]
        # Latest event: pull the stacktrace summary if present.
        try:
            event = await self._request("GET", f"/issues/{issue_id}/events/latest/")
            frames = _top_frames(event)
            if frames:
                out.append("\n--- latest event: top frames ---")
                out.extend(frames)
        except RuntimeError:
            pass
        return "\n".join(out)

    async def _set_state(self, kwargs: dict, session_key: str, action: str) -> str:
        issue_id = (kwargs.get("issue_id") or "").strip()
        if not issue_id:
            return f"Error: {action} requires issue_id."
        status = "resolved" if action == "resolve_issue" else "ignored"
        if not await self._require_approval(f"Set Sentry issue {issue_id} to {status}", session_key):
            return f"Cancelled — issue {issue_id} unchanged."
        issue = await self._request("PUT", f"/issues/{issue_id}/", json={"status": status})
        return f"Issue {issue.get('shortId', issue_id)} is now {issue.get('status', status)}."


def _top_frames(event: Any, limit: int = 8) -> list[str]:
    """Extract a compact top-of-stack summary from a Sentry event payload."""
    if not isinstance(event, dict):
        return []
    entries = event.get("entries") or []
    for entry in entries:
        if entry.get("type") != "exception":
            continue
        values = (entry.get("data") or {}).get("values") or []
        for val in values:
            frames = (val.get("stacktrace") or {}).get("frames") or []
            # Sentry lists frames oldest→newest; show the last (innermost) ones.
            picked = frames[-limit:]
            lines = []
            for f in picked:
                loc = f.get("filename") or f.get("module") or "?"
                fn = f.get("function") or "?"
                lineno = f.get("lineNo")
                where = f"{loc}:{lineno}" if lineno else loc
                lines.append(f"  {where} in {fn}")
            if lines:
                head = f"{val.get('type', 'Exception')}: {val.get('value', '')}".strip()
                return [head, *lines] if head else lines
    return []
