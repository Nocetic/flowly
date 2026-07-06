"""Best-effort read of the signed-in Flowly account's credit balance.

Lets the TUI /usage screen surface the SAME credits Flowly Desktop shows
(``GET /api/billing/credits`` — see Desktop's ``useBilling.ts``). Read-only and
strictly best-effort: never raises, short timeout, any failure just means
/usage falls back to the local token/cost view without a credits line.
"""

from __future__ import annotations

import os
from typing import Any

# Same host account_key.py mints against; overridable for staging via the env
# var the web tool already honors.
_BASE = os.environ.get("FLOWLY_API_BASE", "https://useflowlyapp.com").rstrip("/")
_CREDITS_ENDPOINT = f"{_BASE}/api/billing/credits"


async def fetch_account_credits(account: Any) -> dict | None:
    """Fetch the signed-in account's ``CreditInfo`` JSON, or ``None``.

    Returns the parsed body (``{"credits": {...}, "plan": {...}, ...}``) on a
    200, else ``None`` — including when not signed in (no id token), on any
    HTTP/network error, or on a non-JSON body. Bounded to a few seconds so a
    slow/offline backend never hangs the /usage modal.
    """
    token = (getattr(account, "id_token", "") or "").strip()
    if not token:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0)) as client:
            resp = await client.get(
                _CREDITS_ENDPOINT,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None
