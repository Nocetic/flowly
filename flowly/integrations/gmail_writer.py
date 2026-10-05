"""Explicit, bounded Gmail management with one approval for an exact message set."""

import asyncio
import json
import unicodedata

import httpx

from flowly.channels import gmail_auth
from flowly.integrations.gmail_connection import GmailConnection
from flowly.integrations.gmail_reader import (
    API,
    GmailReader,
    GmailReadError,
    message_summary,
    valid_id,
)
from flowly.integrations.google_permissions import granted_services

LABEL_ACTIONS = {
    "archive": ([], ["INBOX"]), "move_to_inbox": (["INBOX"], []),
    "mark_read": ([], ["UNREAD"]), "mark_unread": (["UNREAD"], []),
    "star": (["STARRED"], []), "unstar": ([], ["STARRED"]),
}
WRITE_ACTIONS = frozenset({"trash", "untrash", "label", "unlabel", *LABEL_ACTIONS})


def validate_write(action: str, args: dict) -> list[str]:
    if action not in WRITE_ACTIONS:
        raise ValueError("Unknown Gmail management action.")
    if set(args) - {"message_id", "message_ids", "label_ids", "session_key"}:
        raise ValueError("Management accepts explicit message IDs, never a search query or thread ID.")
    if "message_id" in args and "message_ids" in args:
        raise ValueError("Use message_id or message_ids, not both.")
    ids = args.get("message_ids", [args.get("message_id")])
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not valid_id(item) for item in ids):
        raise ValueError("Provide 1–100 valid message IDs from inbox/search/read results.")
    labels = args.get("label_ids")
    if action in {"label", "unlabel"}:
        if not isinstance(labels, list) or not 1 <= len(labels) <= 20 or any(not valid_id(item) for item in labels):
            raise ValueError("Provide 1–20 existing user label IDs from labels_list.")
    elif labels is not None:
        raise ValueError("label_ids is only accepted for label/unlabel.")
    return list(dict.fromkeys(ids))


def _text(value, limit=180):
    # Headers are untrusted data, including newlines, terminal escapes and bidi controls.
    return ''.join(ch if not unicodedata.category(ch).startswith('C') else ' ' for ch in str(value or ''))[:limit]


async def manage_mail(action: str, args: dict, approve) -> str:
    try:
        ids = validate_write(action, args)
    except ValueError as error:
        return f"Error: INVALID_ARGUMENT: {error}"
    from flowly.agent.tool_context import current_tool_origin
    origin = current_tool_origin()
    if origin is not None:
        args = {**args, "session_key": origin.session_key}
    token, email = await asyncio.to_thread(gmail_auth.get_valid_access_token)
    credentials = gmail_auth.load_credentials()
    if not token or not credentials:
        return "Error: AUTH_REQUIRED: Use google_connection to request Gmail access."
    if "gmail_manage" not in granted_services(credentials):
        return "Error: PERMISSION_REQUIRED: Request Gmail management access with google_connection; existing read/send access is unchanged."
    connection_id = GmailConnection.connection_id(credentials)
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            reader = GmailReader(client, token)
            semaphore = asyncio.Semaphore(5)

            async def summary(msg_id):
                async with semaphore:
                    data = await reader.get(f"messages/{msg_id}", [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")])
                    return message_summary(data, msg_id)

            async with asyncio.timeout(90):
                messages = await asyncio.gather(*(summary(msg_id) for msg_id in ids))
                label_names = []
                if action in {"label", "unlabel"}:
                    data = await reader.get("labels")
                    labels = {item["id"]: item.get("name", item["id"]) for item in data.get("labels", [])
                              if isinstance(item, dict) and item.get("type") == "user" and valid_id(item.get("id"))}
                    if any(item not in labels for item in args["label_ids"]):
                        return "Error: INVALID_ARGUMENT: Only existing user labels may be applied or removed."
                    label_names = [_text(labels[item]) for item in args["label_ids"]]
            description = f"Gmail: {action} · {len(ids)} message(s) · {_text(email)}"
            if action == "trash":
                description += "\nMove to Trash; no immediate permanent deletion."
            if label_names:
                description += "\nLabels: " + ", ".join(label_names)
            description += "\nMessage headers (untrusted data):\n" + "\n".join(
                f"• {_text(item['subject'])} — {_text(item['from'])} [ID: {item['id']}]" for item in messages)
            if not await approve(description, args.get("session_key", "")):
                return json.dumps({"action": action, "status": "cancelled", "requested": len(ids), "succeeded": [], "failed": [], "not_attempted": ids})
            # Approval is bound to the account AND local grant that the user reviewed.
            token, refreshed_email = await asyncio.to_thread(gmail_auth.get_valid_access_token)
            latest = gmail_auth.load_credentials()
            if (not token or not latest or refreshed_email != email
                    or GmailConnection.connection_id(latest) != connection_id
                    or "gmail_manage" not in granted_services(latest)):
                return "Error: CONNECTION_CHANGED: Gmail access changed while waiting for approval. No messages were modified."
            result = {"action": action, "status": "completed", "requested": len(ids), "succeeded": [], "failed": [], "not_attempted": []}
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 180
            for index, msg_id in enumerate(ids):
                current = gmail_auth.load_credentials()
                if (loop.time() >= deadline or not current or current.get("disconnect_pending") or current.get("reauthorize_required")
                        or GmailConnection.connection_id(current) != connection_id):
                    result["not_attempted"] = ids[index:]
                    break
                body = None
                operation = action if action in {"trash", "untrash"} else "modify"
                if operation == "modify":
                    add, remove = LABEL_ACTIONS.get(action, (args.get("label_ids", []) if action == "label" else [], args.get("label_ids", []) if action == "unlabel" else []))
                    body = {"addLabelIds": add, "removeLabelIds": remove}
                try:
                    # Do not replay a write after an ambiguous network/server failure.
                    async with client.stream("POST", f"{API}/messages/{msg_id}/{operation}", headers={"Authorization": f"Bearer {token}"}, json=body, timeout=20) as response:
                        if response.status_code == 200:
                            result["succeeded"].append(msg_id)
                            continue
                        code = {400: "INVALID_REQUEST", 401: "AUTH_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMITED"}.get(response.status_code, "OUTCOME_UNKNOWN")
                except httpx.HTTPError:
                    code = "OUTCOME_UNKNOWN"
                result["failed"].append({"id": msg_id, "code": code})
                if code in {"AUTH_REQUIRED", "FORBIDDEN", "RATE_LIMITED", "OUTCOME_UNKNOWN"}:
                    result["not_attempted"] = ids[index + 1:]
                    break
            if result["failed"] or result["not_attempted"]:
                result["status"] = "partial" if result["succeeded"] else "incomplete"
                result["next_step"] = "Read failed/unknown IDs to verify current state before retrying; do not repeat successful IDs."
            return json.dumps(result, ensure_ascii=False)
    except GmailReadError as error:
        return f"Error: {error}. No messages were modified."
    except (TimeoutError, httpx.HTTPError):
        return "Error: UNAVAILABLE: Could not prepare the Gmail approval. No messages were modified."
    except (ValueError, TypeError, KeyError):
        return "Error: INVALID_RESPONSE: Could not validate Gmail message metadata. No messages were modified."
