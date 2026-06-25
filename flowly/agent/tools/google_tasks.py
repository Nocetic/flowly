"""Google Tasks tool — manage task lists and tasks.

Uses OAuth tokens from ~/.flowly/credentials/gmail.json.
Creating/updating/deleting tasks requires user approval.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.channels import gmail_auth

_TASKS_API = "https://tasks.googleapis.com/tasks/v1"


class GoogleTasksTool(Tool):
    """Manage Google Tasks."""

    @property
    def name(self) -> str:
        return "google_tasks"

    @property
    def description(self) -> str:
        return (
            "Manage Google Tasks. "
            "Actions: lists (show task lists), tasks (list tasks in a list), "
            "create (add task), complete (mark done), delete (remove task). "
            "Only use when the user explicitly asks about tasks/todos."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["lists", "tasks", "create", "complete", "delete"],
                    "description": "Action to perform.",
                },
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID (default: @default).",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (for complete/delete).",
                },
                "title": {
                    "type": "string",
                    "description": "Task title (for create).",
                },
                "notes": {
                    "type": "string",
                    "description": "Task notes/description (for create).",
                },
                "due": {
                    "type": "string",
                    "description": "Due date in ISO 8601 (for create).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max tasks to return (default 20).",
                },
            },
            "required": ["action"],
        }

    async def _require_approval(self, description: str, session_key: str = "") -> bool:
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
            logger.error(f"[Tasks] Approval error: {e}")
            return False

    async def execute(self, action: str, **kwargs: Any) -> str:
        token, _ = gmail_auth.get_valid_access_token()
        if not token:
            return "Error: Google account not connected."

        tl_id = kwargs.get("tasklist_id", "@default")

        if action == "lists":
            return await self._list_tasklists(token)
        elif action == "tasks":
            return await self._list_tasks(token, tl_id, kwargs.get("max_results", 20))
        elif action == "create":
            return await self._create_task(token, tl_id, kwargs)
        elif action == "complete":
            return await self._complete_task(token, tl_id, kwargs)
        elif action == "delete":
            return await self._delete_task(token, tl_id, kwargs)
        return f"Error: Unknown action '{action}'."

    async def _list_tasklists(self, token: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_TASKS_API}/users/@me/lists",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                items = resp.json().get("items", [])
                if not items:
                    return "No task lists found."
                lines = ["Task lists:\n"]
                for tl in items:
                    lines.append(f"📋 {tl.get('title', '?')} (ID: {tl.get('id', '?')})")
                return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    async def _list_tasks(self, token: str, tl_id: str, max_results: int) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_TASKS_API}/lists/{tl_id}/tasks",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"maxResults": str(min(max_results, 100)), "showCompleted": "false"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                items = resp.json().get("items", [])
                if not items:
                    return "No pending tasks."
                lines = [f"Tasks ({len(items)}):\n"]
                for t in items:
                    status = "✅" if t.get("status") == "completed" else "⬜"
                    due = t.get("due", "")
                    due_str = f" (due: {due[:10]})" if due else ""
                    lines.append(f"{status} {t.get('title', '?')}{due_str}")
                    lines.append(f"   ID: {t.get('id', '?')}")
                    if t.get("notes"):
                        lines.append(f"   Notes: {t['notes'][:100]}")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    async def _create_task(self, token: str, tl_id: str, kwargs: dict) -> str:
        title = kwargs.get("title", "")
        if not title:
            return "Error: 'title' required."

        approved = await self._require_approval(
            f"✅ Create task: {title}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Task creation cancelled — user denied."

        body: dict[str, Any] = {"title": title}
        if kwargs.get("notes"):
            body["notes"] = kwargs["notes"]
        if kwargs.get("due"):
            body["due"] = kwargs["due"]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_TASKS_API}/lists/{tl_id}/tasks",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                    timeout=10,
                )
                if resp.status_code in (200, 201):
                    t = resp.json()
                    return f"Task created: {t.get('title')} (ID: {t.get('id')})"
                return f"Error ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"

    async def _complete_task(self, token: str, tl_id: str, kwargs: dict) -> str:
        task_id = kwargs.get("task_id", "")
        if not task_id:
            return "Error: task_id required."
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.patch(
                    f"{_TASKS_API}/lists/{tl_id}/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"status": "completed"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return f"Task marked as completed."
                return f"Error ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"

    async def _delete_task(self, token: str, tl_id: str, kwargs: dict) -> str:
        task_id = kwargs.get("task_id", "")
        if not task_id:
            return "Error: task_id required."

        approved = await self._require_approval(
            f"🗑️ Delete task {task_id[:12]}...",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Task deletion cancelled — user denied."

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{_TASKS_API}/lists/{tl_id}/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if resp.status_code == 204:
                    return "Task deleted."
                return f"Error ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"
