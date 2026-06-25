"""Email tool — read and send Gmail on demand.

NOT a channel — agent only accesses email when explicitly asked.
Uses OAuth tokens from ~/.flowly/credentials/gmail.json.

Send/reply ALWAYS require user approval regardless of exec security
settings. Uses the same approval UI as the exec tool.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.channels import gmail_auth

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class EmailTool(Tool):
    """Read and send emails via Gmail API.

    Send/reply ALWAYS require approval — no config can disable this.
    Uses the exact same approval system as exec tool.
    """

    @property
    def name(self) -> str:
        return "email"

    @property
    def description(self) -> str:
        return (
            "Read and send emails via Gmail. "
            "Actions: inbox (list recent emails), read (get full email by ID), "
            "send (send a new email — supports file attachments), "
            "reply (reply to an email — supports file attachments). "
            "Only use when the user explicitly asks about emails."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["inbox", "read", "send", "reply", "search"],
                    "description": "Action to perform.",
                },
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID (for read/reply).",
                },
                "to": {
                    "type": "string",
                    "description": "Recipient email address (for send).",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject (for send).",
                },
                "body": {
                    "type": "string",
                    "description": "Email body text (for send/reply).",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search). Gmail search syntax.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max emails to return (default 5).",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "File paths to attach to the email (for send/reply). "
                        "Each path must be an absolute path to a readable file. "
                        "Gmail limit: 35 MB total per email."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        token, email = gmail_auth.get_valid_access_token()
        if not token:
            return "Error: Gmail not connected. Connect via Desktop app settings."

        if action == "inbox":
            return await self._list_inbox(token, kwargs.get("max_results", 5))
        elif action == "read":
            msg_id = kwargs.get("message_id", "")
            if not msg_id:
                return "Error: message_id required for read action."
            return await self._read_message(token, msg_id)
        elif action == "send":
            return await self._send(token, email or "", kwargs)
        elif action == "reply":
            return await self._reply(token, email or "", kwargs)
        elif action == "search":
            query = kwargs.get("query", "")
            if not query:
                return "Error: query required for search action."
            return await self._search(token, query, kwargs.get("max_results", 5))
        else:
            return f"Error: Unknown action '{action}'. Use: inbox, read, send, reply, search."

    async def _require_approval(self, description: str, session_key: str = "") -> bool:
        """ALWAYS require approval for email send/reply.

        Uses the exact same ExecApprovalStore as the exec tool.
        Cannot be disabled by config — email send always asks.
        """
        from flowly.exec.approval_manager import get_approval_manager
        from flowly.exec.types import PendingApproval, ExecRequest
        import secrets

        approval_mgr = get_approval_manager()

        pending = PendingApproval(
            id=secrets.token_hex(8),
            request=ExecRequest(command=description),
            created_at=time.time(),
            expires_at=time.time() + 120,
            session_key=session_key,
            # A sent email can't be "remembered" — only allow-once/deny make
            # sense, so don't let surfaces offer a no-op "Always allow".
            supports_always=False,
        )

        try:
            decision = await approval_mgr.request_and_wait(pending)
            if decision is None:
                logger.info("[Email] Approval timed out — denying")
                return False
            if decision == "deny":
                logger.info("[Email] User denied email send")
                return False
            logger.info(f"[Email] User approved: {decision}")
            return True
        except Exception as e:
            logger.error(f"[Email] Approval error: {e}")
            return False

    async def _list_inbox(self, token: str, max_results: int) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_GMAIL_API}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": "in:inbox", "maxResults": str(min(max_results, 10))},
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: Gmail API returned {resp.status_code}"

                messages = resp.json().get("messages", [])
                if not messages:
                    return "Inbox is empty."

                results = []
                for msg_ref in messages[:max_results]:
                    detail = await self._get_headers(client, token, msg_ref["id"])
                    if detail:
                        results.append(detail)

                lines = [f"Found {len(results)} emails:\n"]
                for r in results:
                    unread = "📩" if r.get("unread") else "📧"
                    lines.append(f"{unread} ID: {r['id']}")
                    lines.append(f"   From: {r['from']}")
                    lines.append(f"   Subject: {r['subject']}")
                    lines.append(f"   Date: {r['date']}")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error reading inbox: {e}"

    async def _get_headers(self, client: httpx.AsyncClient, token: str, msg_id: str) -> dict | None:
        try:
            resp = await client.get(
                f"{_GMAIL_API}/messages/{msg_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"format": "metadata", "metadataHeaders": "From,Subject,Date"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
            labels = data.get("labelIds", [])
            return {
                "id": msg_id,
                "from": headers.get("from", "?"),
                "subject": headers.get("subject", "(no subject)"),
                "date": headers.get("date", "?"),
                "unread": "UNREAD" in labels,
            }
        except Exception:
            return None

    async def _read_message(self, token: str, msg_id: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_GMAIL_API}/messages/{msg_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"format": "full"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: Gmail API returned {resp.status_code}"

                data = resp.json()
                headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
                body = self._extract_body(data.get("payload", {}))

                return (
                    f"From: {headers.get('from', '?')}\n"
                    f"To: {headers.get('to', '?')}\n"
                    f"Subject: {headers.get('subject', '(no subject)')}\n"
                    f"Date: {headers.get('date', '?')}\n"
                    f"\n{body or '(empty body)'}"
                )
        except Exception as e:
            return f"Error reading message: {e}"

    def _validate_attachments(self, paths: list[str]) -> tuple[list[Path], str | None]:
        """Validate attachment paths. Returns (valid_paths, error_or_none)."""
        validated: list[Path] = []
        total_size = 0
        max_total = 35 * 1024 * 1024  # Gmail 35 MB limit

        for p in paths:
            fp = Path(p).expanduser().resolve()
            if not fp.exists():
                return [], f"Attachment not found: {fp}"
            if not fp.is_file():
                return [], f"Not a file: {fp}"
            size = fp.stat().st_size
            if size == 0:
                return [], f"Empty file: {fp}"
            total_size += size
            if total_size > max_total:
                return [], f"Total attachment size exceeds Gmail's 35 MB limit ({total_size // (1024*1024)} MB)"
            validated.append(fp)

        return validated, None

    def _get_sender_name(self, from_email: str) -> str:
        """Get the user's display name for the email signature.

        Priority:
          1. workspace/USER.md first line (usually a "# FullName" header)
          2. Gmail address local-part (the part before ``@``)
        """
        try:
            from flowly.profile import get_flowly_home
            user_md = get_flowly_home() / "workspace" / "USER.md"
            if user_md.exists():
                content = user_md.read_text(encoding="utf-8").strip()
                # First line is usually "# Name" or just "Name"
                first_line = content.split("\n")[0].strip().lstrip("#").strip()
                if first_line and len(first_line) < 60:
                    return first_line
        except Exception:
            pass

        # Fallback: email prefix, capitalized
        name = from_email.split("@")[0]
        return name.replace(".", " ").replace("_", " ").title()

    def _append_footer(self, body: str, from_email: str) -> str:
        """Append the sender name + Flowly footer to the email body.

        The LLM writes the sign-off in the appropriate language (the system
        prompt tells it to include a proper closing). We only add:
          - Sender's real name (so the recipient knows who sent it)
          - "Flowly Agent ile gönderildi" branding footer

        This is the ONLY programmatic addition — everything else (greeting,
        body, sign-off language) is the LLM's responsibility.
        """
        sender_name = self._get_sender_name(from_email)
        return f"{body.rstrip()}\n\n{sender_name}\n\n--\nSent via Flowly"

    def _build_mime_message(
        self,
        from_email: str,
        to: str,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        """Build a MIME message and return base64url-encoded raw string."""
        # Append sender name + Flowly footer
        body = self._append_footer(body, from_email)

        if attachments:
            mime_msg = MIMEMultipart()
            mime_msg.attach(MIMEText(body, "plain", "utf-8"))
            for fp in attachments:
                content_type, _ = mimetypes.guess_type(str(fp))
                if content_type is None:
                    content_type = "application/octet-stream"
                main_type, sub_type = content_type.split("/", 1)
                part = MIMEBase(main_type, sub_type)
                part.set_payload(fp.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", "attachment", filename=fp.name,
                )
                mime_msg.attach(part)
        else:
            mime_msg = MIMEText(body, "plain", "utf-8")

        mime_msg["To"] = to
        mime_msg["From"] = from_email
        mime_msg["Subject"] = subject
        if in_reply_to:
            mime_msg["In-Reply-To"] = in_reply_to
            mime_msg["References"] = references or in_reply_to

        return base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")

    async def _send(self, token: str, from_email: str, kwargs: dict) -> str:
        to = kwargs.get("to", "")
        subject = kwargs.get("subject", "")
        body = kwargs.get("body", "")
        attachment_paths: list[str] = kwargs.get("attachments", []) or []

        if not to:
            return "Error: 'to' (recipient email) is required."
        if not body:
            return "Error: 'body' (message text) is required."

        # Validate attachments before asking for approval
        valid_attachments: list[Path] = []
        if attachment_paths:
            valid_attachments, err = self._validate_attachments(attachment_paths)
            if err:
                return f"Error: {err}"

        # ALWAYS require approval — cannot be disabled
        preview = body[:100] + ("..." if len(body) > 100 else "")
        attach_info = ""
        if valid_attachments:
            names = ", ".join(fp.name for fp in valid_attachments)
            total_kb = sum(fp.stat().st_size for fp in valid_attachments) // 1024
            attach_info = f"\n📎 Attachments ({total_kb} KB): {names}"

        approved = await self._require_approval(
            f"📧 Send email to {to}\nSubject: {subject or '(no subject)'}{attach_info}\n\n{preview}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Email send cancelled — user denied approval."

        raw = self._build_mime_message(
            from_email, to, subject or "(no subject)", body,
            attachments=valid_attachments or None,
        )

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_GMAIL_API}/messages/send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"raw": raw},
                    timeout=60 if valid_attachments else 30,
                )
                if resp.status_code == 200:
                    attach_note = f" with {len(valid_attachments)} attachment(s)" if valid_attachments else ""
                    return f"Email sent to {to}{attach_note}."
                else:
                    return f"Error sending email ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            return f"Error sending email: {e}"

    async def _reply(self, token: str, from_email: str, kwargs: dict) -> str:
        msg_id = kwargs.get("message_id", "")
        body = kwargs.get("body", "")
        attachment_paths: list[str] = kwargs.get("attachments", []) or []

        if not msg_id:
            return "Error: 'message_id' is required to reply."
        if not body:
            return "Error: 'body' (reply text) is required."

        # Validate attachments before asking for approval
        valid_attachments: list[Path] = []
        if attachment_paths:
            valid_attachments, err = self._validate_attachments(attachment_paths)
            if err:
                return f"Error: {err}"

        # ALWAYS require approval — cannot be disabled
        preview = body[:100] + ("..." if len(body) > 100 else "")
        attach_info = ""
        if valid_attachments:
            names = ", ".join(fp.name for fp in valid_attachments)
            total_kb = sum(fp.stat().st_size for fp in valid_attachments) // 1024
            attach_info = f"\n📎 Attachments ({total_kb} KB): {names}"

        approved = await self._require_approval(
            f"📧 Reply to email (ID: {msg_id[:12]}...){attach_info}\n\n{preview}",
            kwargs.get("session_key", ""),
        )
        if not approved:
            return "Email reply cancelled — user denied approval."

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_GMAIL_API}/messages/{msg_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"format": "metadata", "metadataHeaders": "From,Subject,Message-ID"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return f"Error: Could not fetch original message ({resp.status_code})"

                data = resp.json()
                headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
                thread_id = data.get("threadId", "")
                original_from = headers.get("from", "")
                subject = headers.get("subject", "")
                message_id_header = headers.get("message-id", "")

                to_match = re.search(r"<([^>]+)>", original_from)
                to_email = to_match.group(1) if to_match else original_from.strip()

                if not subject.startswith("Re:"):
                    subject = f"Re: {subject}"

                raw = self._build_mime_message(
                    from_email, to_email, subject, body,
                    attachments=valid_attachments or None,
                    in_reply_to=message_id_header,
                    references=message_id_header,
                )

                send_resp = await client.post(
                    f"{_GMAIL_API}/messages/send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"raw": raw, "threadId": thread_id},
                    timeout=60 if valid_attachments else 30,
                )
                if send_resp.status_code == 200:
                    attach_note = f" with {len(valid_attachments)} attachment(s)" if valid_attachments else ""
                    return f"Reply sent to {to_email}{attach_note}."
                else:
                    return f"Error sending reply ({send_resp.status_code}): {send_resp.text[:200]}"
        except Exception as e:
            return f"Error replying: {e}"

    async def _search(self, token: str, query: str, max_results: int) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_GMAIL_API}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": query, "maxResults": str(min(max_results, 10))},
                    timeout=15,
                )
                if resp.status_code != 200:
                    return f"Error: Gmail API returned {resp.status_code}"

                messages = resp.json().get("messages", [])
                if not messages:
                    return f"No emails found for: {query}"

                results = []
                for msg_ref in messages[:max_results]:
                    detail = await self._get_headers(client, token, msg_ref["id"])
                    if detail:
                        results.append(detail)

                lines = [f"Found {len(results)} emails matching '{query}':\n"]
                for r in results:
                    lines.append(f"📧 ID: {r['id']}")
                    lines.append(f"   From: {r['from']}")
                    lines.append(f"   Subject: {r['subject']}")
                    lines.append("")
                return "\n".join(lines)
        except Exception as e:
            return f"Error searching: {e}"

    def _extract_body(self, payload: dict) -> str:
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                padded = data + "=" * (4 - len(data) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    padded = data + "=" * (4 - len(data) % 4)
                    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            elif part.get("mimeType", "").startswith("multipart/"):
                nested = self._extract_body(part)
                if nested:
                    return nested

        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    padded = data + "=" * (4 - len(data) % 4)
                    html = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
                    text = re.sub(r"<[^>]+>", "", text)
                    return text.strip()
        return ""
