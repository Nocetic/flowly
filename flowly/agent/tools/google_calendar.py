"""Google Calendar tool — manage calendar events on demand.

Uses OAuth tokens from ~/.flowly/credentials/gmail.json.
Creating/updating/deleting events requires user approval.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.channels import gmail_auth

_CALENDAR_API = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarTool(Tool):
    """Manage Google Calendar events."""

    @property
    def name(self) -> str:
        return "google_calendar"

    @property
    def description(self) -> str:
        return (
            "Manage Google Calendar. "
            "Actions: list (upcoming events), get (event details), "
            "create (new event), update (modify event), delete (remove event), "
            "search (find events by query). "
            "Only use when the user explicitly asks about calendar/schedule."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "search"],
                    "description": "Action to perform.",
                },
                "event_id": {
                    "type": "string",
                    "description": "Event ID (for get/update/delete).",
                },
                "summary": {
                    "type": "string",
                    "description": "Event title (for create/update).",
                },
                "description": {
                    "type": "string",
                    "description": "Event description (for create/update).",
                },
                "start": {
                    "type": "string",
                    "description": "Start datetime in ISO 8601 (e.g., 2026-04-10T14:00:00+03:00).",
                },
                "end": {
                    "type": "string",
                    "description": "End datetime in ISO 8601 (e.g., 2026-04-10T15:00:00+03:00).",
                },
                "location": {
                    "type": "string",
                    "description": "Event location (for create/update).",
                },
                "attendees": {
                    "type": "string",
                    "description": "Comma-separated email addresses of attendees.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search action).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (default 10).",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID (default: primary).",
                },
            },
            "required": ["action"],
        }

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
            logger.error(f"[Calendar] Approval error: {e}")
            return False

    async def execute(self, action: str, **kwargs: Any) -> str:
        token, _ = gmail_auth.get_valid_access_token()
        if not token:
            return "Error: Google account not connected. Connect via Desktop app settings."

        cal_id = kwargs.get("calendar_id", "primary")

        if action == "list":
            return await self._list_events(token, cal_id, kwargs.get("max_results", 10))
        elif action == "get":
            return await self._get_event(token, cal_id, kwargs.get("event_id", ""))
        elif action == "create":
            return await self._create_event(token, cal_id, kwargs)
        elif action == "update":
            return await self._update_event(token, cal_id, kwargs)
        elif action == "delete":
            return await self._delete_event(token, cal_id, kwargs)
        elif action == "search":
            return await self._search_events(token, cal_id, kwargs.get("query", ""), kwargs.get("max_results", 10))
        return f"Error: Unknown action '{action}'."

    async def _list_events(self, token: str, cal_id: str, max_results: int) -> str:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_CALENDAR_API}/calendars/{cal_id}/events",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "timeMin": now,
                        "maxResults": str(min(max_results, 25)),
                        "singleEvents": "true",
                        "orderBy": "startTime",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: Calendar API returned {resp.status_code}"
                events = resp.json().get("items", [])
                if not events:
                    return "No upcoming events."
                lines = [f"Upcoming events ({len(events)}):\n"]
                for e in events:
                    start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
                    lines.append(f"📅 {e.get('summary', '(no title)')}")
                    lines.append(f"   When: {start}")
                    lines.append(f"   ID: {e.get('id', '?')}")
                    if e.get("location"):
                        lines.append(f"   Where: {e['location']}")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    async def _get_event(self, token: str, cal_id: str, event_id: str) -> str:
        if not event_id:
            return "Error: event_id required."
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_CALENDAR_API}/calendars/{cal_id}/events/{event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                e = resp.json()
                start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
                end = e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "?"))
                attendees = ", ".join(a.get("email", "") for a in e.get("attendees", []))
                return (
                    f"📅 {e.get('summary', '(no title)')}\n"
                    f"When: {start} → {end}\n"
                    f"Where: {e.get('location', '—')}\n"
                    f"Description: {e.get('description', '—')}\n"
                    f"Attendees: {attendees or '—'}\n"
                    f"Link: {e.get('htmlLink', '—')}"
                )
        except Exception as e:
            return f"Error: {e}"

    async def _create_event(self, token: str, cal_id: str, kwargs: dict) -> str:
        summary = kwargs.get("summary", "")
        start = kwargs.get("start", "")
        end = kwargs.get("end", "")
        if not summary or not start:
            return "Error: 'summary' and 'start' are required."
        if not end:
            end = start  # Default: same as start (will be adjusted by API)

        approved = await self._require_approval(
            f"📅 Create calendar event\nTitle: {summary}\nWhen: {start}\nLocation: {kwargs.get('location', '—')}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Event creation cancelled — user denied."

        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if kwargs.get("description"):
            body["description"] = kwargs["description"]
        if kwargs.get("location"):
            body["location"] = kwargs["location"]
        if kwargs.get("attendees"):
            body["attendees"] = [{"email": e.strip()} for e in kwargs["attendees"].split(",")]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_CALENDAR_API}/calendars/{cal_id}/events",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    e = resp.json()
                    return f"Event created: {e.get('summary')} (ID: {e.get('id')})\nLink: {e.get('htmlLink', '')}"
                return f"Error creating event ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"

    async def _update_event(self, token: str, cal_id: str, kwargs: dict) -> str:
        event_id = kwargs.get("event_id", "")
        if not event_id:
            return "Error: event_id required."

        approved = await self._require_approval(
            f"📅 Update calendar event {event_id[:12]}...\nChanges: {', '.join(k for k in ('summary','start','end','location','description') if kwargs.get(k))}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Event update cancelled — user denied."

        body: dict[str, Any] = {}
        if kwargs.get("summary"):
            body["summary"] = kwargs["summary"]
        if kwargs.get("start"):
            body["start"] = {"dateTime": kwargs["start"]}
        if kwargs.get("end"):
            body["end"] = {"dateTime": kwargs["end"]}
        if kwargs.get("description"):
            body["description"] = kwargs["description"]
        if kwargs.get("location"):
            body["location"] = kwargs["location"]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.patch(
                    f"{_CALENDAR_API}/calendars/{cal_id}/events/{event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                    timeout=15,
                )
                if resp.status_code == 200:
                    return f"Event updated: {resp.json().get('summary', '')}"
                return f"Error updating event ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"

    async def _delete_event(self, token: str, cal_id: str, kwargs: dict) -> str:
        event_id = kwargs.get("event_id", "")
        if not event_id:
            return "Error: event_id required."

        approved = await self._require_approval(
            f"📅 Delete calendar event {event_id[:12]}...",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Event deletion cancelled — user denied."

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{_CALENDAR_API}/calendars/{cal_id}/events/{event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if resp.status_code == 204:
                    return "Event deleted."
                return f"Error deleting event ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"

    async def _search_events(self, token: str, cal_id: str, query: str, max_results: int) -> str:
        if not query:
            return "Error: query required."
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_CALENDAR_API}/calendars/{cal_id}/events",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": query, "maxResults": str(min(max_results, 25)), "singleEvents": "true", "orderBy": "startTime",
                            "timeMin": "2020-01-01T00:00:00Z"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                events = resp.json().get("items", [])
                if not events:
                    return f"No events found for: {query}"
                lines = [f"Found {len(events)} events:\n"]
                for e in events:
                    start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
                    lines.append(f"📅 {e.get('summary', '(no title)')} — {start}")
                    lines.append(f"   ID: {e.get('id', '?')}")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"
