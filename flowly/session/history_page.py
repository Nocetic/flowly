"""Additive display-history paging with stable, session-bound cursors."""
from __future__ import annotations

import base64
import json

from flowly.session.archive import EVENT_ID_KEY, legacy_event_id, message_fingerprint


class HistoryPageError(ValueError):
    code = "INVALID_HISTORY_CURSOR"


def history_page(rows: list[dict], session_key: str, params: dict) -> tuple[list[dict], dict]:
    if "limit" not in params and "before" not in params:
        return rows, {}  # Legacy wire contract remains unchanged.
    limit = params.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise HistoryPageError("History page size must be between 1 and 200.")
    identities: list[str] = []
    occurrences: dict[str, int] = {}
    for row in rows:
        fingerprint = message_fingerprint(row)
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        identities.append(str(row.get(EVENT_ID_KEY) or legacy_event_id(row, occurrence)))
    end = len(rows)
    before = params.get("before")
    if before is not None:
        try:
            if not isinstance(before, str) or not 1 <= len(before) <= 2048:
                raise ValueError()
            cursor = json.loads(base64.b64decode(before.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(cursor, list) or len(cursor) != 3 or cursor[:2] != [1, session_key]:
                raise ValueError()
            end = identities.index(cursor[2])
        except (ValueError, UnicodeError, TypeError) as exc:
            raise HistoryPageError("This history page is no longer available. Reopen the conversation to refresh it.") from exc
    start = max(0, end - limit)
    # Avoid splitting ordinary tool chains, but bound pathological long turns.
    while start > max(0, end - 200) and rows[start].get("role") != "user":
        start -= 1
    page = [{**row, "id": row.get("id") or identities[index]} for index, row in enumerate(rows[start:end], start)]
    cursor = base64.urlsafe_b64encode(json.dumps([1, session_key, identities[start]]).encode()).decode() if start > 0 else None
    return page, {"historyPageVersion": 1, "hasOlder": start > 0, "before": cursor}
