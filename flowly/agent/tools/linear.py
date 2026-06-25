"""Linear tool — manage issues, projects, and teams via GraphQL API.

Uses personal API key from config (integrations.linear.api_key).
Creating/updating issues and adding comments require user approval.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool

_LINEAR_API = "https://api.linear.app/graphql"


class LinearTool(Tool):
    """Manage Linear issues, projects, and teams."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "linear"

    @property
    def description(self) -> str:
        return (
            "Manage Linear project management. "
            "Actions: list_issues (list issues with filters), get_issue (details by ID/key), "
            "create_issue (new issue), update_issue (modify issue), "
            "add_comment (comment on issue), search (full-text search), "
            "list_projects (list projects), list_teams (list teams). "
            "Only use when the user explicitly asks about Linear."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_issues", "get_issue", "create_issue",
                        "update_issue", "add_comment", "search",
                        "list_projects", "list_teams",
                    ],
                    "description": "Action to perform.",
                },
                "issue_id": {
                    "type": "string",
                    "description": "Issue identifier (UUID or key like 'ENG-123') for get/update/comment.",
                },
                "title": {
                    "type": "string",
                    "description": "Issue title (for create_issue).",
                },
                "description": {
                    "type": "string",
                    "description": "Issue description in markdown (for create/update).",
                },
                "team_id": {
                    "type": "string",
                    "description": "Team ID (for create_issue, list_issues filter).",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project ID (for create_issue, list_issues filter).",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "User ID to assign (for create/update).",
                },
                "state_name": {
                    "type": "string",
                    "description": "Workflow state name like 'In Progress', 'Done' (for update).",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority: 0=none, 1=urgent, 2=high, 3=medium, 4=low (for create/update).",
                },
                "label_names": {
                    "type": "string",
                    "description": "Comma-separated label names (for create/update).",
                },
                "comment_body": {
                    "type": "string",
                    "description": "Comment text in markdown (for add_comment).",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search action).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 10).",
                },
                "status_filter": {
                    "type": "string",
                    "description": "Filter by state name, e.g. 'Todo', 'In Progress' (for list_issues).",
                },
            },
            "required": ["action"],
        }

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query against Linear API."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _LINEAR_API,
                headers={
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
                timeout=20,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Linear API returned {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if data.get("errors"):
                msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
                raise RuntimeError(f"Linear GraphQL error: {msgs}")
            return data.get("data", {})

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    async def _require_approval(self, description: str, session_key: str = "") -> bool:
        """Require approval for write operations."""
        from flowly.exec.approval_manager import get_approval_manager
        from flowly.exec.types import PendingApproval, ExecRequest
        import secrets

        pending = PendingApproval(
            id=secrets.token_hex(8),
            request=ExecRequest(command=description),
            created_at=time.time(),
            expires_at=time.time() + 120,
            session_key=session_key,
            # This write can't be remembered — don't offer a no-op "Always
            # allow" (allow-once / deny only).
            supports_always=False,
        )
        try:
            decision = await get_approval_manager().request_and_wait(pending)
            if decision is None or decision == "deny":
                return False
            return True
        except Exception as e:
            logger.error(f"[Linear] Approval error: {e}")
            return False

    # ------------------------------------------------------------------
    # Execute dispatcher
    # ------------------------------------------------------------------

    async def execute(self, action: str, **kwargs: Any) -> str:
        try:
            if action == "list_issues":
                return await self._list_issues(kwargs)
            elif action == "get_issue":
                return await self._get_issue(kwargs.get("issue_id", ""))
            elif action == "create_issue":
                return await self._create_issue(kwargs)
            elif action == "update_issue":
                return await self._update_issue(kwargs)
            elif action == "add_comment":
                return await self._add_comment(kwargs)
            elif action == "search":
                return await self._search(kwargs.get("query", ""), kwargs.get("max_results", 10))
            elif action == "list_projects":
                return await self._list_projects(kwargs.get("max_results", 20))
            elif action == "list_teams":
                return await self._list_teams()
            return f"Error: Unknown action '{action}'."
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _list_issues(self, kwargs: dict) -> str:
        max_results = min(kwargs.get("max_results", 10), 50)
        team_id = kwargs.get("team_id", "")
        project_id = kwargs.get("project_id", "")
        status_filter = kwargs.get("status_filter", "")

        # Build filter
        filters: list[str] = []
        if team_id:
            filters.append(f'team: {{ id: {{ eq: "{team_id}" }} }}')
        if project_id:
            filters.append(f'project: {{ id: {{ eq: "{project_id}" }} }}')
        if status_filter:
            filters.append(f'state: {{ name: {{ eqCaseInsensitive: "{status_filter}" }} }}')

        filter_str = ", ".join(filters)
        filter_arg = f", filter: {{ {filter_str} }}" if filter_str else ""

        query = f"""
        query {{
            issues(first: {max_results}{filter_arg}, orderBy: updatedAt) {{
                nodes {{
                    id
                    identifier
                    title
                    state {{ name }}
                    priority
                    assignee {{ name }}
                    team {{ name key }}
                    project {{ name }}
                    updatedAt
                }}
            }}
        }}
        """
        data = await self._gql(query)
        issues = data.get("issues", {}).get("nodes", [])
        if not issues:
            return "No issues found."

        lines = [f"Found {len(issues)} issues:\n"]
        for i in issues:
            priority_map = {0: "-", 1: "!!!", 2: "!!", 3: "!", 4: "."}
            p = priority_map.get(i.get("priority", 0), "-")
            state = i.get("state", {}).get("name", "?")
            assignee = i.get("assignee", {})
            assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
            team = i.get("team", {}).get("key", "?")
            project = i.get("project")
            project_name = project.get("name", "") if project else ""

            lines.append(f"[{p}] {i.get('identifier', '?')} — {i.get('title', '(no title)')}")
            lines.append(f"    State: {state} | Assignee: {assignee_name} | Team: {team}")
            if project_name:
                lines.append(f"    Project: {project_name}")
            lines.append(f"    ID: {i.get('id', '?')}")
            lines.append("")
        return "\n".join(lines)

    async def _get_issue(self, issue_id: str) -> str:
        if not issue_id:
            return "Error: issue_id required."

        # Try by identifier first (e.g. ENG-123), then by UUID
        query = """
        query($id: String!) {
            issueSearch(first: 1, filter: { or: [
                { id: { eq: $id } },
                { number: { eq: 0 } }
            ] }) {
                nodes { id }
            }
            issue(id: $id) {
                id
                identifier
                title
                description
                state { name }
                priority
                assignee { name email }
                team { name key }
                project { name }
                labels { nodes { name } }
                createdAt
                updatedAt
                url
                comments(first: 5) {
                    nodes {
                        body
                        user { name }
                        createdAt
                    }
                }
            }
        }
        """
        # Linear accepts both UUID and identifier for issue() query
        # but identifier needs a different approach
        try:
            data = await self._gql("query($id: String!) { issue(id: $id) { id identifier title description state { name } priority assignee { name email } team { name key } project { name } labels { nodes { name } } createdAt updatedAt url comments(first: 5) { nodes { body user { name } createdAt } } } }", {"id": issue_id})
            issue = data.get("issue")
        except Exception:
            # Might be an identifier like "ENG-123", search for it
            data = await self._gql(
                'query($q: String!) { issueSearch(first: 1, query: $q) { nodes { id identifier title description state { name } priority assignee { name email } team { name key } project { name } labels { nodes { name } } createdAt updatedAt url comments(first: 5) { nodes { body user { name } createdAt } } } } }',
                {"q": issue_id},
            )
            nodes = data.get("issueSearch", {}).get("nodes", [])
            issue = nodes[0] if nodes else None

        if not issue:
            return f"Issue '{issue_id}' not found."

        priority_map = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
        labels = ", ".join(l.get("name", "") for l in issue.get("labels", {}).get("nodes", []))
        assignee = issue.get("assignee")
        assignee_str = f"{assignee['name']} ({assignee.get('email', '')})" if assignee else "Unassigned"
        project = issue.get("project")

        lines = [
            f"# {issue.get('identifier', '?')} — {issue.get('title', '')}",
            f"State: {issue.get('state', {}).get('name', '?')}",
            f"Priority: {priority_map.get(issue.get('priority', 0), '?')}",
            f"Assignee: {assignee_str}",
            f"Team: {issue.get('team', {}).get('name', '?')} ({issue.get('team', {}).get('key', '')})",
        ]
        if project:
            lines.append(f"Project: {project.get('name', '?')}")
        if labels:
            lines.append(f"Labels: {labels}")
        lines.append(f"Created: {issue.get('createdAt', '?')}")
        lines.append(f"Updated: {issue.get('updatedAt', '?')}")
        lines.append(f"URL: {issue.get('url', '?')}")

        desc = issue.get("description", "")
        if desc:
            # Trim long descriptions
            if len(desc) > 2000:
                desc = desc[:2000] + "\n[... truncated]"
            lines.append(f"\n## Description\n{desc}")

        comments = issue.get("comments", {}).get("nodes", [])
        if comments:
            lines.append(f"\n## Recent Comments ({len(comments)})")
            for c in comments:
                user = c.get("user", {}).get("name", "?")
                lines.append(f"\n**{user}** ({c.get('createdAt', '?')[:10]}):")
                body = c.get("body", "")
                if len(body) > 500:
                    body = body[:500] + "..."
                lines.append(body)

        return "\n".join(lines)

    async def _create_issue(self, kwargs: dict) -> str:
        title = kwargs.get("title", "")
        team_id = kwargs.get("team_id", "")
        if not title:
            return "Error: 'title' is required."
        if not team_id:
            return "Error: 'team_id' is required. Use list_teams to find available teams."

        approved = await self._require_approval(
            f"📋 Create Linear issue\nTitle: {title}\nTeam: {team_id[:12]}...",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Issue creation cancelled — user denied."

        input_fields: dict[str, Any] = {
            "title": title,
            "teamId": team_id,
        }
        if kwargs.get("description"):
            input_fields["description"] = kwargs["description"]
        if kwargs.get("assignee_id"):
            input_fields["assigneeId"] = kwargs["assignee_id"]
        if kwargs.get("project_id"):
            input_fields["projectId"] = kwargs["project_id"]
        if kwargs.get("priority") is not None:
            input_fields["priority"] = kwargs["priority"]

        # Resolve state by name if provided
        if kwargs.get("state_name"):
            state_id = await self._resolve_state_id(team_id, kwargs["state_name"])
            if state_id:
                input_fields["stateId"] = state_id

        # Resolve labels by name
        if kwargs.get("label_names"):
            label_ids = await self._resolve_label_ids(team_id, kwargs["label_names"])
            if label_ids:
                input_fields["labelIds"] = label_ids

        mutation = """
        mutation($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue {
                    id
                    identifier
                    title
                    url
                }
            }
        }
        """
        data = await self._gql(mutation, {"input": input_fields})
        result = data.get("issueCreate", {})
        if not result.get("success"):
            return "Error: Failed to create issue."
        issue = result.get("issue", {})
        return f"Issue created: {issue.get('identifier')} — {issue.get('title')}\nURL: {issue.get('url', '')}\nID: {issue.get('id', '')}"

    async def _update_issue(self, kwargs: dict) -> str:
        issue_id = kwargs.get("issue_id", "")
        if not issue_id:
            return "Error: issue_id required."

        # Resolve issue ID if it's a key like "ENG-123"
        resolved_id = await self._resolve_issue_id(issue_id)
        if not resolved_id:
            return f"Error: Issue '{issue_id}' not found."

        changes = []
        input_fields: dict[str, Any] = {}

        if kwargs.get("title"):
            input_fields["title"] = kwargs["title"]
            changes.append(f"title → {kwargs['title']}")
        if kwargs.get("description"):
            input_fields["description"] = kwargs["description"]
            changes.append("description updated")
        if kwargs.get("assignee_id"):
            input_fields["assigneeId"] = kwargs["assignee_id"]
            changes.append(f"assignee → {kwargs['assignee_id'][:12]}...")
        if kwargs.get("priority") is not None:
            input_fields["priority"] = kwargs["priority"]
            priority_map = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
            changes.append(f"priority → {priority_map.get(kwargs['priority'], '?')}")
        if kwargs.get("state_name"):
            # Need team_id to resolve state — get it from the issue
            issue_data = await self._gql(
                "query($id: String!) { issue(id: $id) { team { id } } }",
                {"id": resolved_id},
            )
            team_id = issue_data.get("issue", {}).get("team", {}).get("id", "")
            if team_id:
                state_id = await self._resolve_state_id(team_id, kwargs["state_name"])
                if state_id:
                    input_fields["stateId"] = state_id
                    changes.append(f"state → {kwargs['state_name']}")

        if not input_fields:
            return "Error: No fields to update. Provide title, description, assignee_id, state_name, or priority."

        approved = await self._require_approval(
            f"📋 Update Linear issue {issue_id}\nChanges: {', '.join(changes)}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Issue update cancelled — user denied."

        mutation = """
        mutation($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) {
                success
                issue {
                    identifier
                    title
                    state { name }
                }
            }
        }
        """
        data = await self._gql(mutation, {"id": resolved_id, "input": input_fields})
        result = data.get("issueUpdate", {})
        if not result.get("success"):
            return "Error: Failed to update issue."
        issue = result.get("issue", {})
        return f"Issue updated: {issue.get('identifier')} — {issue.get('title')} [{issue.get('state', {}).get('name', '?')}]"

    async def _add_comment(self, kwargs: dict) -> str:
        issue_id = kwargs.get("issue_id", "")
        body = kwargs.get("comment_body", "")
        if not issue_id:
            return "Error: issue_id required."
        if not body:
            return "Error: comment_body required."

        resolved_id = await self._resolve_issue_id(issue_id)
        if not resolved_id:
            return f"Error: Issue '{issue_id}' not found."

        preview = body[:100] + ("..." if len(body) > 100 else "")
        approved = await self._require_approval(
            f"📋 Add comment to {issue_id}\n\n{preview}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Comment cancelled — user denied."

        mutation = """
        mutation($input: CommentCreateInput!) {
            commentCreate(input: $input) {
                success
                comment { id }
            }
        }
        """
        data = await self._gql(mutation, {"input": {"issueId": resolved_id, "body": body}})
        if not data.get("commentCreate", {}).get("success"):
            return "Error: Failed to add comment."
        return f"Comment added to {issue_id}."

    async def _search(self, query: str, max_results: int) -> str:
        if not query:
            return "Error: query required."

        gql = """
        query($q: String!, $first: Int!) {
            issueSearch(first: $first, query: $q) {
                nodes {
                    id
                    identifier
                    title
                    state { name }
                    priority
                    assignee { name }
                    team { key }
                }
            }
        }
        """
        data = await self._gql(gql, {"q": query, "first": min(max_results, 50)})
        issues = data.get("issueSearch", {}).get("nodes", [])
        if not issues:
            return f"No issues found for: {query}"

        priority_map = {0: "-", 1: "!!!", 2: "!!", 3: "!", 4: "."}
        lines = [f"Found {len(issues)} issues matching '{query}':\n"]
        for i in issues:
            p = priority_map.get(i.get("priority", 0), "-")
            state = i.get("state", {}).get("name", "?")
            assignee = i.get("assignee")
            assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
            lines.append(f"[{p}] {i.get('identifier', '?')} — {i.get('title', '')}")
            lines.append(f"    State: {state} | Assignee: {assignee_name} | Team: {i.get('team', {}).get('key', '?')}")
            lines.append(f"    ID: {i.get('id', '?')}")
            lines.append("")
        return "\n".join(lines)

    async def _list_projects(self, max_results: int) -> str:
        query = f"""
        query {{
            projects(first: {min(max_results, 50)}, orderBy: updatedAt) {{
                nodes {{
                    id
                    name
                    state
                    teams {{ nodes {{ name key }} }}
                    issues {{ nodes {{ id }} }}
                    updatedAt
                }}
            }}
        }}
        """
        data = await self._gql(query)
        projects = data.get("projects", {}).get("nodes", [])
        if not projects:
            return "No projects found."

        lines = [f"Found {len(projects)} projects:\n"]
        for p in projects:
            teams = ", ".join(t.get("key", "?") for t in p.get("teams", {}).get("nodes", []))
            issue_count = len(p.get("issues", {}).get("nodes", []))
            lines.append(f"📂 {p.get('name', '?')} ({p.get('state', '?')})")
            lines.append(f"   Teams: {teams or '—'} | Issues: {issue_count}")
            lines.append(f"   ID: {p.get('id', '?')}")
            lines.append("")
        return "\n".join(lines)

    async def _list_teams(self) -> str:
        query = """
        query {
            teams {
                nodes {
                    id
                    name
                    key
                    members { nodes { id name email } }
                    states { nodes { id name type } }
                }
            }
        }
        """
        data = await self._gql(query)
        teams = data.get("teams", {}).get("nodes", [])
        if not teams:
            return "No teams found."

        lines = [f"Found {len(teams)} teams:\n"]
        for t in teams:
            members = ", ".join(m.get("name", "?") for m in t.get("members", {}).get("nodes", []))
            states = ", ".join(s.get("name", "?") for s in t.get("states", {}).get("nodes", []))
            lines.append(f"👥 {t.get('name', '?')} ({t.get('key', '?')})")
            lines.append(f"   Members: {members or '—'}")
            lines.append(f"   States: {states or '—'}")
            lines.append(f"   ID: {t.get('id', '?')}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_issue_id(self, issue_id: str) -> str | None:
        """Resolve an issue identifier (UUID or key like ENG-123) to a UUID."""
        # If it looks like a UUID, return as-is
        if len(issue_id) > 20 and "-" in issue_id:
            return issue_id
        # Search by identifier
        try:
            data = await self._gql(
                'query($q: String!) { issueSearch(first: 1, query: $q) { nodes { id } } }',
                {"q": issue_id},
            )
            nodes = data.get("issueSearch", {}).get("nodes", [])
            return nodes[0]["id"] if nodes else None
        except Exception:
            return None

    async def _resolve_state_id(self, team_id: str, state_name: str) -> str | None:
        """Resolve a workflow state name to its ID for a given team."""
        try:
            data = await self._gql(
                'query($teamId: String!) { team(id: $teamId) { states { nodes { id name } } } }',
                {"teamId": team_id},
            )
            states = data.get("team", {}).get("states", {}).get("nodes", [])
            name_lower = state_name.lower()
            for s in states:
                if s.get("name", "").lower() == name_lower:
                    return s["id"]
            return None
        except Exception:
            return None

    async def _resolve_label_ids(self, team_id: str, label_names: str) -> list[str]:
        """Resolve comma-separated label names to IDs."""
        try:
            data = await self._gql(
                'query($teamId: String!) { team(id: $teamId) { labels { nodes { id name } } } }',
                {"teamId": team_id},
            )
            labels = data.get("team", {}).get("labels", {}).get("nodes", [])
            names = [n.strip().lower() for n in label_names.split(",")]
            return [l["id"] for l in labels if l.get("name", "").lower() in names]
        except Exception:
            return []
