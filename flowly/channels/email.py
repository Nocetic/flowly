"""Email channel — Gmail API polling + send via OAuth 2.0.

Polls Gmail inbox for unread messages, dispatches them to the agent,
and sends replies as threaded email responses.  Authentication uses
OAuth tokens stored at ``~/.flowly/credentials/gmail.json`` — the user
never provides a password.
"""

from __future__ import annotations

import asyncio
import base64
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.base import BaseChannel
from flowly.channels import gmail_auth

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Automated sender patterns to skip (prevent reply loops)
_AUTOMATED_PATTERNS = re.compile(
    r"noreply|no-reply|no_reply|donotreply|do-not-reply|"
    r"mailer-daemon|postmaster|bounce|notifications@|automated@|"
    r"calendar-notification|feedback@|updates@|alert@|"
    r"news@|newsletter@|digest@",
    re.IGNORECASE,
)


def _strip_html(html: str) -> str:
    """Basic HTML → plain text conversion."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')
    return text.strip()


def _decode_base64url(data: str) -> str:
    """Decode base64url-encoded string (Gmail API format)."""
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_email(from_header: str) -> str:
    """Extract email address from 'Name <email>' format."""
    match = re.search(r"<([^>]+)>", from_header)
    return match.group(1) if match else from_header.strip()


def _extract_name(from_header: str) -> str:
    """Extract display name from 'Name <email>' format."""
    match = re.match(r"^([^<]+)<", from_header)
    if match:
        name = match.group(1).strip().strip('"')
        return name if name else _extract_email(from_header)
    return _extract_email(from_header)


class EmailChannel(BaseChannel):
    """Gmail channel using OAuth + REST API."""

    name = "email"

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        self._poll_interval = getattr(config, "poll_interval_seconds", 30)
        self._seen_ids: set[str] = set()
        self._max_seen = 2000
        self._thread_context: dict[str, dict[str, str]] = {}  # chat_id → {subject, message_id}
        self._my_email: str | None = None
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start Gmail polling loop."""
        token, email = gmail_auth.get_valid_access_token()
        if not token:
            logger.warning("[Email] No Gmail credentials found — channel disabled. "
                           "Connect Gmail via web app or desktop app.")
            return

        self._my_email = email
        self._running = True
        logger.info(f"[Email] Channel started (polling every {self._poll_interval}s, account: {email})")

        while self._running:
            try:
                await self._poll_inbox()
            except Exception as e:
                logger.error(f"[Email] Poll error: {e}")
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("[Email] Channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a reply email via Gmail API."""
        token, _ = gmail_auth.get_valid_access_token()
        if not token:
            logger.error("[Email] Cannot send — no valid token")
            return

        to_email = msg.chat_id
        subject = "Re: (no subject)"
        in_reply_to = None
        references = None

        # Thread context for proper email threading
        ctx = self._thread_context.get(to_email)
        if ctx:
            subject = ctx.get("subject", subject)
            if not subject.startswith("Re:"):
                subject = f"Re: {subject}"
            in_reply_to = ctx.get("message_id")
            references = in_reply_to

        # Build MIME message
        if msg.media:
            mime_msg = MIMEMultipart()
            mime_msg.attach(MIMEText(msg.content, "plain", "utf-8"))
            for media_path in msg.media:
                path = Path(media_path)
                if path.exists():
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(path.read_bytes())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={path.name}")
                    mime_msg.attach(part)
        else:
            mime_msg = MIMEText(msg.content, "plain", "utf-8")

        mime_msg["To"] = to_email
        mime_msg["From"] = self._my_email or ""
        mime_msg["Subject"] = subject
        if in_reply_to:
            mime_msg["In-Reply-To"] = in_reply_to
        if references:
            mime_msg["References"] = references

        # Base64url encode for Gmail API
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_GMAIL_API}/messages/send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"raw": raw},
                    timeout=30,
                )
                if resp.status_code == 200:
                    logger.info(f"[Email] Sent reply to {to_email}")
                else:
                    logger.error(f"[Email] Send failed ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            logger.error(f"[Email] Send error: {e}")

    # ── Polling ────────────────────────────────────────────────────

    async def _poll_inbox(self) -> None:
        """Check for unread emails and dispatch to agent."""
        token, _ = gmail_auth.get_valid_access_token()
        if not token:
            return

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{_GMAIL_API}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": "is:unread in:inbox", "maxResults": "10"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f"[Email] List messages failed ({resp.status_code})")
                    return

                data = resp.json()
                messages = data.get("messages", [])

                for msg_ref in messages:
                    msg_id = msg_ref["id"]
                    if msg_id in self._seen_ids:
                        continue

                    await self._process_message(client, token, msg_id)
                    self._seen_ids.add(msg_id)

                    # Trim seen set
                    if len(self._seen_ids) > self._max_seen:
                        excess = len(self._seen_ids) - self._max_seen
                        for _ in range(excess):
                            self._seen_ids.pop()

        except Exception as e:
            logger.error(f"[Email] Inbox poll error: {e}")

    async def _process_message(self, client: httpx.AsyncClient, token: str, msg_id: str) -> None:
        """Fetch, parse, and dispatch a single email."""
        resp = await client.get(
            f"{_GMAIL_API}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"[Email] Fetch message {msg_id} failed ({resp.status_code})")
            return

        msg_data = resp.json()
        headers = {h["name"].lower(): h["value"] for h in msg_data.get("payload", {}).get("headers", [])}

        from_header = headers.get("from", "")
        sender_email = _extract_email(from_header)
        sender_name = _extract_name(from_header)
        subject = headers.get("subject", "(no subject)")
        message_id = headers.get("message-id", "")
        thread_id = msg_data.get("threadId", msg_id)

        # Skip automated senders
        if _AUTOMATED_PATTERNS.search(sender_email):
            logger.debug(f"[Email] Skipping automated sender: {sender_email}")
            await self._mark_read(client, token, msg_id)
            return

        # Skip self-messages (prevent reply loops)
        if self._my_email and sender_email.lower() == self._my_email.lower():
            logger.debug("[Email] Skipping self-message")
            await self._mark_read(client, token, msg_id)
            return

        # Extract body
        body = self._extract_body(msg_data.get("payload", {}))
        if not body:
            body = "(empty email)"

        # Build content with subject context
        content = body
        if subject and not subject.startswith("Re:"):
            content = f"[Subject: {subject}]\n\n{body}"

        # Store thread context for reply threading
        self._thread_context[sender_email] = {
            "subject": subject,
            "message_id": message_id,
        }

        # Mark as read
        await self._mark_read(client, token, msg_id)

        # Dispatch to agent
        logger.info(f"[Email] New email from {sender_name} <{sender_email}>: {subject[:60]}")
        await self._handle_message(
            sender_id=sender_email,
            chat_id=sender_email,
            content=content,
            metadata={
                "sender_name": sender_name,
                "subject": subject,
                "message_id": message_id,
                "thread_id": thread_id,
                "gmail_msg_id": msg_id,
            },
        )

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        mime_type = payload.get("mimeType", "")

        # Simple text/plain
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return _decode_base64url(data)

        # Multipart — look for text/plain first, then text/html
        parts = payload.get("parts", [])
        text_parts: list[str] = []
        html_parts: list[str] = []

        for part in parts:
            part_mime = part.get("mimeType", "")
            if part_mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    text_parts.append(_decode_base64url(data))
            elif part_mime == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    html_parts.append(_decode_base64url(data))
            elif part_mime.startswith("multipart/"):
                # Nested multipart — recurse
                nested = self._extract_body(part)
                if nested:
                    text_parts.append(nested)

        if text_parts:
            return "\n".join(text_parts)
        if html_parts:
            return _strip_html("\n".join(html_parts))
        return ""

    async def _mark_read(self, client: httpx.AsyncClient, token: str, msg_id: str) -> None:
        """Mark a message as read."""
        try:
            await client.post(
                f"{_GMAIL_API}/messages/{msg_id}/modify",
                headers={"Authorization": f"Bearer {token}"},
                json={"removeLabelIds": ["UNREAD"]},
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"[Email] Mark read failed for {msg_id}: {e}")
