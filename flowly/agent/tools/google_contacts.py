"""Google Contacts tool — search and read contacts.

Uses OAuth tokens from ~/.flowly/credentials/gmail.json.
Read-only — no write operations, no approval needed.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.channels import gmail_auth

_PEOPLE_API = "https://people.googleapis.com/v1"


class GoogleContactsTool(Tool):
    """Search and read Google Contacts."""

    @property
    def name(self) -> str:
        return "google_contacts"

    @property
    def description(self) -> str:
        return (
            "Search and read Google Contacts. "
            "Actions: search (find contacts by name/email), "
            "list (recent contacts). "
            "Read-only — no approval needed. "
            "Only use when the user explicitly asks about contacts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "list"],
                    "description": "Action to perform.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (name, email, phone).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max contacts to return (default 10).",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        token, _ = gmail_auth.get_valid_access_token()
        if not token:
            return "Error: Google account not connected."

        if action == "search":
            return await self._search(token, kwargs.get("query", ""), kwargs.get("max_results", 10))
        elif action == "list":
            return await self._list(token, kwargs.get("max_results", 10))
        return f"Error: Unknown action '{action}'."

    async def _search(self, token: str, query: str, max_results: int) -> str:
        if not query:
            return "Error: query required for search."
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_PEOPLE_API}/people:searchContacts",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "query": query,
                        "pageSize": str(min(max_results, 30)),
                        "readMask": "names,emailAddresses,phoneNumbers,organizations",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: Contacts API returned {resp.status_code}"
                results = resp.json().get("results", [])
                if not results:
                    return f"No contacts found for: {query}"
                return self._format_contacts([r.get("person", {}) for r in results])
        except Exception as e:
            return f"Error: {e}"

    async def _list(self, token: str, max_results: int) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_PEOPLE_API}/people/me/connections",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "pageSize": str(min(max_results, 30)),
                        "personFields": "names,emailAddresses,phoneNumbers,organizations",
                        "sortOrder": "LAST_MODIFIED_DESCENDING",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                connections = resp.json().get("connections", [])
                if not connections:
                    return "No contacts found."
                return self._format_contacts(connections)
        except Exception as e:
            return f"Error: {e}"

    def _format_contacts(self, contacts: list[dict]) -> str:
        lines = [f"Contacts ({len(contacts)}):\n"]
        for c in contacts:
            names = c.get("names", [])
            name = names[0].get("displayName", "?") if names else "?"
            emails = [e.get("value", "") for e in c.get("emailAddresses", [])]
            phones = [p.get("value", "") for p in c.get("phoneNumbers", [])]
            orgs = [o.get("name", "") for o in c.get("organizations", [])]

            lines.append(f"👤 {name}")
            if emails:
                lines.append(f"   Email: {', '.join(emails)}")
            if phones:
                lines.append(f"   Phone: {', '.join(phones)}")
            if orgs:
                lines.append(f"   Org: {', '.join(orgs)}")
            lines.append("")
        return "\n".join(lines)
