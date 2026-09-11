"""Bounded, read-only Gmail API operations. No token persistence or send retries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import random
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import httpx

API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_PAGE = 100
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 30_000
MAX_CONCURRENCY = 5
_ID = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")


class GmailReadError(Exception):
    """Safe public error; never include upstream body, URL, credentials or query."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def headers_of(payload: dict) -> dict[str, str]:
    result = {}
    items = payload.get("headers", [])
    if not isinstance(items, list):
        raise GmailReadError("INVALID_RESPONSE", "Message headers are invalid.")
    for item in items:
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            try:
                value = str(make_header(decode_header(value)))
            except (LookupError, ValueError, UnicodeError):
                pass
            result[name.lower()] = value
    return result


def message_summary(data: dict, msg_id: str) -> dict:
    payload = data.get("payload")
    if data.get("id") != msg_id or not isinstance(payload, dict):
        raise GmailReadError("INVALID_RESPONSE", "Gmail returned an invalid message.")
    headers = headers_of(payload)
    received_at = None
    try:
        received_at = datetime.fromtimestamp(
            int(data["internalDate"]) / 1000, timezone.utc
        ).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    labels = data.get("labelIds", [])
    if not isinstance(labels, list):
        labels = []
    return {
        "id": msg_id,
        "thread_id": data.get("threadId"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date") or received_at,
        "received_at": received_at,
        "snippet": data.get("snippet", ""),
        "labels": labels,
        "unread": "UNREAD" in labels,
        "missing_headers": [name for name in ("from", "subject", "date") if name not in headers],
    }


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.hidden = 0
        self.links: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "template"}:
            self.hidden += 1
        if not self.hidden and tag in {
            "br",
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "blockquote",
        }:
            self.text.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.text.append("\t")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append(
                href
                if not self.hidden and href.lower().startswith(("https://", "http://", "mailto:"))
                else None
            )

    def handle_endtag(self, tag):
        if tag == "a" and self.links:
            href = self.links.pop()
            if href and not self.hidden:
                self.text.append(f" <{href}>")
        if tag in {"script", "style", "head", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "blockquote"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def body_parts(payload: dict) -> tuple[list[dict], list[dict]]:
    """Choose plain over HTML per MIME alternative; do not read attached files."""
    attachments: list[dict] = []
    visited = 0

    def walk(part: dict, depth: int) -> list[dict]:
        nonlocal visited
        visited += 1
        if visited > 500 or depth > 30 or not isinstance(part, dict):
            raise GmailReadError("INVALID_RESPONSE", "Message MIME structure is too complex.")
        mime = str(part.get("mimeType", "")).lower()
        headers = headers_of(part)
        body = part.get("body") or {}
        if not isinstance(body, dict):
            raise GmailReadError("INVALID_RESPONSE", "Message body metadata is invalid.")
        if part.get("filename") or headers.get("content-disposition", "").lower().startswith(
            "attachment"
        ):
            attachments.append(
                {
                    "filename": part.get("filename", ""),
                    "mime_type": mime,
                    "size": body.get("size"),
                    "attachment_id": body.get("attachmentId"),
                }
            )
            return []
        if mime in {"text/plain", "text/html"}:
            return [part] if body.get("data") or body.get("attachmentId") else []
        if not mime.startswith("multipart/"):
            return []
        children = part.get("parts", [])
        if not isinstance(children, list):
            raise GmailReadError("INVALID_RESPONSE", "Message MIME parts are invalid.")
        branches = [walk(child, depth + 1) for child in children]
        if mime == "multipart/alternative":
            for branch in branches:
                if any(str(item.get("mimeType", "")).lower() == "text/plain" for item in branch):
                    return branch
            return next((branch for branch in branches if branch), [])
        return [item for branch in branches for item in branch]

    return walk(payload, 0), attachments


def decode_part(part: dict, data: str, warnings: list[str]) -> str:
    if not isinstance(data, str):
        raise GmailReadError("INVALID_RESPONSE", "Message text data is missing or invalid.")
    if len(data) > (MAX_BODY_BYTES * 4 // 3 + 8):
        raise GmailReadError("BODY_TOO_LARGE", "Message text exceeds the safe reading limit.")
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise GmailReadError("INVALID_RESPONSE", "Message text encoding is invalid.") from None
    if len(raw) > MAX_BODY_BYTES:
        raise GmailReadError("BODY_TOO_LARGE", "Message text exceeds the safe reading limit.")
    content_type = headers_of(part).get("content-type", "")
    mime_header = Message()
    mime_header["content-type"] = content_type
    charset = mime_header.get_content_charset() or "utf-8"
    try:
        text = raw.decode(charset)
    except (LookupError, UnicodeError):
        text = raw.decode("utf-8", errors="replace")
        warnings.append("Some characters could not be decoded with the declared character set.")
    if str(part.get("mimeType", "")).lower() == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.text)).strip()
    return text


class GmailReader:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.token = token

    async def get(self, path: str, params: Any = None) -> dict:
        # Only idempotent GETs use retry. Sending mail never passes through here.
        for attempt in range(3):
            response = None
            try:
                async with self.client.stream(
                    "GET",
                    f"{API}/{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=15,
                    follow_redirects=False,
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise GmailReadError(
                                "RESPONSE_TOO_LARGE",
                                "Gmail response exceeds the safe reading limit.",
                            )
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError):
                        data = None
                if response.status_code == 200:
                    if not isinstance(data, dict):
                        raise GmailReadError("INVALID_RESPONSE", "Gmail returned invalid data.")
                    return data
                reasons = set()
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    errors = data["error"].get("errors", [])
                    if isinstance(errors, list):
                        reasons = {
                            item.get("reason")
                            for item in errors
                            if isinstance(item, dict) and isinstance(item.get("reason"), str)
                        }
                status = response.status_code
                retry = (
                    status == 429
                    or status in {500, 502, 503, 504}
                    or (
                        status == 403
                        and bool(
                            reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
                        )
                    )
                )
                if status == 401:
                    error = GmailReadError(
                        "AUTH_REQUIRED",
                        "Gmail authorization was rejected. Check the connection and try again.",
                    )
                elif status == 403 and not retry:
                    error = GmailReadError(
                        "FORBIDDEN",
                        "Gmail denied access. Check account permissions or administrator policy.",
                    )
                elif status == 404:
                    error = GmailReadError("NOT_FOUND", "This message is no longer available.")
                elif status == 400:
                    error = GmailReadError(
                        "INVALID_REQUEST",
                        "Gmail rejected the query or page token. Check the search syntax; restart pagination if needed.",
                    )
                elif retry:
                    error = GmailReadError(
                        "RATE_LIMITED" if status in {403, 429} else "UNAVAILABLE",
                        "Gmail is temporarily unavailable; try again later.",
                    )
                else:
                    error = GmailReadError("UNAVAILABLE", "Gmail could not complete this request.")
                if not retry:
                    raise error
            except (httpx.TimeoutException, httpx.TransportError):
                error = GmailReadError(
                    "UNAVAILABLE", "Gmail could not be reached; try again later."
                )
            if attempt == 2:
                raise error
            delay = 0.5 * (2**attempt) + random.uniform(0, 0.2)
            retry_after = response.headers.get("Retry-After") if response is not None else None
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(
                            delay,
                            (
                                parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                            ).total_seconds(),
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass
                if not math.isfinite(delay) or delay > 5:
                    # Respect longer server backoff without parking the agent turn.
                    raise error
            await asyncio.sleep(delay)
        raise AssertionError("Unreachable")

    async def list_messages(
        self,
        *,
        query: str,
        max_results: int,
        page_token: str = "",
        inbox: bool = False,
        include_spam_trash: bool = False,
    ) -> dict:
        params: dict[str, Any] = {
            "maxResults": max_results,
            "includeSpamTrash": str(include_spam_trash).lower(),
        }
        if query:
            params["q"] = query
        if inbox:
            params["labelIds"] = ["INBOX"]
        if page_token:
            params["pageToken"] = page_token
        page = await self.get("messages", params)
        references = page.get("messages", [])
        if not isinstance(references, list) or len(references) > max_results:
            raise GmailReadError("INVALID_RESPONSE", "Gmail returned an invalid result page.")
        ids = []
        for item in references:
            if not isinstance(item, dict) or not valid_id(item.get("id")):
                raise GmailReadError(
                    "INVALID_RESPONSE", "Gmail returned an invalid message reference."
                )
            if item["id"] not in ids:
                ids.append(item["id"])
        next_token = page.get("nextPageToken") or None
        if next_token is not None and (
            not isinstance(next_token, str) or len(next_token) > 4096 or next_token == page_token
        ):
            raise GmailReadError(
                "INVALID_RESPONSE", "Gmail returned an invalid continuation token."
            )
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        page_error: GmailReadError | None = None

        async def detail(msg_id):
            nonlocal page_error
            async with semaphore:
                if page_error is not None:
                    return None, {
                        "id": msg_id,
                        "code": page_error.code,
                        "message": page_error.detail,
                        "not_attempted": True,
                    }
                try:
                    data = await self.get(
                        f"messages/{msg_id}",
                        {
                            "format": "metadata",
                            "metadataHeaders": ["From", "To", "Subject", "Date"],
                        },
                    )
                    return message_summary(data, msg_id), None
                except GmailReadError as error:
                    if error.code in {"AUTH_REQUIRED", "FORBIDDEN", "RATE_LIMITED", "UNAVAILABLE"}:
                        # Stop queued requests on account-wide failures. In-flight
                        # reads finish, but a 100-message page must not hammer quota.
                        page_error = error
                    return None, {"id": msg_id, "code": error.code, "message": error.detail}

        details = await asyncio.gather(*(detail(msg_id) for msg_id in ids))
        messages = [item for item, _ in details if item is not None]
        errors = [error for _, error in details if error is not None]
        return {
            "status": "partial" if errors else "ok",
            "query": query,
            "inbox_only": inbox,
            "listed_count": len(ids),
            "returned_count": len(messages),
            "total_estimate": page.get("resultSizeEstimate"),
            "has_more": bool(next_token),
            "next_page_token": next_token,
            "note": "Counts describe this page, not the entire mailbox. Continue with next_page_token as page_token and the same query/filters. Retry failed message IDs with read; missing details do not mean no matches.",
            "errors": errors,
            "messages": messages,
        }

    async def read_message(
        self, msg_id: str, *, body_offset: int = 0, body_limit: int = 12000
    ) -> dict:
        data = await self.get(f"messages/{msg_id}", {"format": "full"})
        result = message_summary(data, msg_id)
        payload = data.get("payload") or {}
        parts, attachments = body_parts(payload)
        texts, warnings = [], []
        total = 0
        for part in parts:
            body = part.get("body") or {}
            encoded = body.get("data")
            if not encoded and body.get("attachmentId"):
                attachment_id = body["attachmentId"]
                if not valid_id(attachment_id):
                    raise GmailReadError("INVALID_RESPONSE", "Message body reference is invalid.")
                if isinstance(body.get("size"), int) and body["size"] > MAX_BODY_BYTES:
                    raise GmailReadError(
                        "BODY_TOO_LARGE", "Message text exceeds the safe reading limit."
                    )
                encoded = (await self.get(f"messages/{msg_id}/attachments/{attachment_id}")).get(
                    "data"
                )
            text = decode_part(part, encoded, warnings)
            total += len(text.encode("utf-8"))
            if total > MAX_BODY_BYTES:
                raise GmailReadError(
                    "BODY_TOO_LARGE", "Message text exceeds the safe reading limit."
                )
            texts.append(text)
        text = "\n\n".join(texts)
        if not parts:
            warnings.append(
                "No readable text body was found; attachments may still contain content."
            )
        if body_offset > len(text):
            raise GmailReadError("INVALID_ARGUMENT", "body_offset exceeds the message text length.")
        end = min(body_offset + body_limit, len(text))
        result.update(
            {
                "status": "ok",
                "body_offset": body_offset,
                "body_length": len(text),
                "body_truncated": end < len(text),
                "next_body_offset": end if end < len(text) else None,
                "attachments": attachments,
                "warnings": list(dict.fromkeys(warnings)),
                "body": text[body_offset:end],
            }
        )
        return result
