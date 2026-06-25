"""Google Drive tool — list, search, read, and create files.

Uses OAuth tokens from ~/.flowly/credentials/gmail.json.
File creation/upload requires user approval.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.channels import gmail_auth

_DRIVE_API = "https://www.googleapis.com/drive/v3"


class GoogleDriveTool(Tool):
    """Access Google Drive files."""

    @property
    def name(self) -> str:
        return "google_drive"

    @property
    def description(self) -> str:
        return (
            "Access Google Drive. "
            "Actions: list (recent files), search (find files), "
            "read (get file content), info (file metadata), "
            "create (create new document). "
            "Only use when the user explicitly asks about Drive files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "read", "info", "create"],
                    "description": "Action to perform.",
                },
                "file_id": {
                    "type": "string",
                    "description": "File ID (for read/info).",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search). Drive search syntax.",
                },
                "name": {
                    "type": "string",
                    "description": "File name (for create).",
                },
                "content": {
                    "type": "string",
                    "description": "File content (for create — plain text).",
                },
                "mime_type": {
                    "type": "string",
                    "description": "MIME type for create (default: text/plain). Use application/vnd.google-apps.document for Google Docs.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max files to return (default 10).",
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
            logger.error(f"[Drive] Approval error: {e}")
            return False

    async def execute(self, action: str, **kwargs: Any) -> str:
        token, _ = gmail_auth.get_valid_access_token()
        if not token:
            return "Error: Google account not connected."

        if action == "list":
            return await self._list_files(token, kwargs.get("max_results", 10))
        elif action == "search":
            return await self._search_files(token, kwargs.get("query", ""), kwargs.get("max_results", 10))
        elif action == "read":
            return await self._read_file(token, kwargs.get("file_id", ""))
        elif action == "info":
            return await self._file_info(token, kwargs.get("file_id", ""))
        elif action == "create":
            return await self._create_file(token, kwargs)
        return f"Error: Unknown action '{action}'."

    async def _list_files(self, token: str, max_results: int) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_DRIVE_API}/files",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "pageSize": str(min(max_results, 25)),
                        "orderBy": "modifiedTime desc",
                        "fields": "files(id,name,mimeType,modifiedTime,size)",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: Drive API returned {resp.status_code}"
                files = resp.json().get("files", [])
                if not files:
                    return "No files found."
                lines = [f"Recent files ({len(files)}):\n"]
                for f in files:
                    icon = "📄" if "document" in f.get("mimeType", "") else "📁" if "folder" in f.get("mimeType", "") else "📎"
                    lines.append(f"{icon} {f['name']}")
                    lines.append(f"   ID: {f['id']}")
                    lines.append(f"   Type: {f.get('mimeType', '?')}")
                    lines.append(f"   Modified: {f.get('modifiedTime', '?')}")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    async def _search_files(self, token: str, query: str, max_results: int) -> str:
        if not query:
            return "Error: query required."
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_DRIVE_API}/files",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "q": f"name contains '{query}' or fullText contains '{query}'",
                        "pageSize": str(min(max_results, 25)),
                        "fields": "files(id,name,mimeType,modifiedTime)",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                files = resp.json().get("files", [])
                if not files:
                    return f"No files found for: {query}"
                lines = [f"Found {len(files)} files:\n"]
                for f in files:
                    lines.append(f"📄 {f['name']} (ID: {f['id']})")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    async def _read_file(self, token: str, file_id: str) -> str:
        if not file_id:
            return "Error: file_id required."
        try:
            async with httpx.AsyncClient() as client:
                # First get file metadata to determine type
                meta_resp = await client.get(
                    f"{_DRIVE_API}/files/{file_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"fields": "name,mimeType"},
                    timeout=10,
                )
                if meta_resp.status_code != 200:
                    return f"Error: {meta_resp.status_code}"
                meta = meta_resp.json()
                mime = meta.get("mimeType", "")

                # Google Docs/Sheets/Slides → export as text
                if "google-apps" in mime:
                    export_mime = "text/plain"
                    if "spreadsheet" in mime:
                        export_mime = "text/csv"
                    resp = await client.get(
                        f"{_DRIVE_API}/files/{file_id}/export",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"mimeType": export_mime},
                        timeout=30,
                    )
                else:
                    # Regular file → download
                    resp = await client.get(
                        f"{_DRIVE_API}/files/{file_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"alt": "media"},
                        timeout=30,
                    )

                if resp.status_code != 200:
                    return f"Error reading file ({resp.status_code})"

                content = resp.text[:50000]  # Cap at 50K chars
                return f"📄 {meta.get('name', '?')}\n\n{content}"
        except Exception as e:
            return f"Error: {e}"

    async def _file_info(self, token: str, file_id: str) -> str:
        if not file_id:
            return "Error: file_id required."
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_DRIVE_API}/files/{file_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"fields": "id,name,mimeType,size,modifiedTime,createdTime,owners,shared,webViewLink"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return f"Error: {resp.status_code}"
                f = resp.json()
                owners = ", ".join(o.get("displayName", o.get("emailAddress", "?")) for o in f.get("owners", []))
                return (
                    f"📄 {f.get('name', '?')}\n"
                    f"Type: {f.get('mimeType', '?')}\n"
                    f"Size: {f.get('size', '?')} bytes\n"
                    f"Created: {f.get('createdTime', '?')}\n"
                    f"Modified: {f.get('modifiedTime', '?')}\n"
                    f"Owner: {owners}\n"
                    f"Shared: {f.get('shared', False)}\n"
                    f"Link: {f.get('webViewLink', '—')}"
                )
        except Exception as e:
            return f"Error: {e}"

    async def _create_file(self, token: str, kwargs: dict) -> str:
        name = kwargs.get("name", "")
        content = kwargs.get("content", "")
        if not name:
            return "Error: 'name' required."

        approved = await self._require_approval(
            f"📄 Create file on Google Drive\nName: {name}\nContent: {content[:80]}...",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "File creation cancelled — user denied."

        mime_type = kwargs.get("mime_type", "text/plain")
        try:
            async with httpx.AsyncClient() as client:
                # Multipart upload: metadata + content
                import json
                metadata = json.dumps({"name": name, "mimeType": mime_type})
                resp = await client.post(
                    "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "multipart/related; boundary=boundary",
                    },
                    content=(
                        b"--boundary\r\n"
                        b"Content-Type: application/json\r\n\r\n"
                        + metadata.encode() + b"\r\n"
                        b"--boundary\r\n"
                        b"Content-Type: " + mime_type.encode() + b"\r\n\r\n"
                        + content.encode() + b"\r\n"
                        b"--boundary--"
                    ),
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    f = resp.json()
                    return f"File created: {f.get('name')} (ID: {f.get('id')})"
                return f"Error ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error: {e}"
