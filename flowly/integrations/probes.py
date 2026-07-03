"""Async health probes for every integration card.

A probe is a coroutine ``(values: dict) -> ProbeResult``. It is invoked
both when the catalog modal opens (to paint status badges) and when the
user hits "Test Connection" inside a setup modal.

Contract
--------
- **Cheap**: total budget ≤ 5 seconds. Use a short httpx timeout.
- **Side-effect-free**: read-only API calls only (GET, auth-test).
- **Never raises**: wrap every exception into a ``ProbeResult``. A probe
  that crashes would block the modal from rendering.
- **Distinguishes errors**: 401 / 403 → ``auth_failed``; network / 5xx /
  timeout → ``down``; missing required field → ``not_configured``;
  ``enabled=False`` → ``disabled``.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx

from flowly.integrations.cards import ProbeResult

_TIMEOUT = httpx.Timeout(4.0, connect=2.0)
_UA = "flowly-tui/integrations-probe"


def _net_error(exc: Exception) -> ProbeResult:
    return ProbeResult("down", f"network: {type(exc).__name__}")


# ── channels ───────────────────────────────────────────────────────


async def probe_telegram(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        if values.get("token"):
            return ProbeResult("disabled", "token set · channel disabled")
        return ProbeResult("not_configured", "no bot token")
    token = (values.get("token") or "").strip()
    if not token:
        return ProbeResult("not_configured", "bot token missing")
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers={"User-Agent": _UA})
    except Exception as exc:
        return _net_error(exc)
    if r.status_code == 401 or r.status_code == 404:
        return ProbeResult("auth_failed", "bot token rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        data = r.json()
        if data.get("ok") and data.get("result", {}).get("username"):
            return ProbeResult("ok", f"@{data['result']['username']}")
    except Exception:
        pass
    return ProbeResult("unknown", "unexpected response")


async def probe_discord(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if values.get("token") else "not_configured",
            "token set · channel disabled" if values.get("token") else "no token",
        )
    token = (values.get("token") or "").strip()
    if not token:
        return ProbeResult("not_configured", "bot token missing")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}", "User-Agent": _UA},
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code in (401, 403):
        return ProbeResult("auth_failed", "bot token rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        data = r.json()
        name = data.get("username")
        if name:
            return ProbeResult("ok", f"as {name}#{data.get('discriminator', '0')}")
    except Exception:
        pass
    return ProbeResult("ok", "authenticated")


async def probe_slack(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if values.get("bot_token") else "not_configured", "channel disabled"
        )
    token = (values.get("bot_token") or "").strip()
    if not token:
        return ProbeResult("not_configured", "bot token missing")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}", "User-Agent": _UA},
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        data = r.json()
        if data.get("ok"):
            team = data.get("team", "?")
            return ProbeResult("ok", f"team {team}")
        # Slack returns {ok: false, error: "invalid_auth"} on bad token.
        return ProbeResult("auth_failed", f"slack: {data.get('error', 'rejected')}")
    except Exception:
        return ProbeResult("unknown", "unexpected response")


async def probe_teams(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if values.get("webhook_url") else "not_configured", "channel disabled"
        )
    url = (values.get("webhook_url") or "").strip()
    if not url:
        return ProbeResult("not_configured", "webhook URL missing")
    if not url.lower().startswith(("https://", "http://")):
        return ProbeResult("auth_failed", "URL must start with https://")
    # Teams webhooks only accept POST with a card. A HEAD/GET will 405
    # but at least confirms the host is reachable.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers={"User-Agent": _UA})
    except Exception as exc:
        return _net_error(exc)
    # 405 Method Not Allowed = endpoint exists; 200/403/404 also acceptable
    # signals that the host is alive.
    if r.status_code in (200, 400, 403, 404, 405):
        return ProbeResult("ok", f"reachable (HTTP {r.status_code})")
    return ProbeResult("down", f"HTTP {r.status_code}")


async def probe_whatsapp(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        return ProbeResult("disabled", "channel disabled")
    url = (values.get("bridge_url") or "").strip()
    if not url:
        return ProbeResult("not_configured", "bridge URL missing")
    # The whatsapp bridge speaks ws://; convert to http:// for a TCP probe.
    http_url = url.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(http_url, headers={"User-Agent": _UA})
    except Exception as exc:
        return _net_error(exc)
    if r.status_code < 500:
        return ProbeResult("ok", f"bridge reachable (HTTP {r.status_code})")
    return ProbeResult("down", f"HTTP {r.status_code}")


async def probe_imessage(values: dict[str, Any]) -> ProbeResult:
    """chat.db readability == Full Disk Access state, checked live."""
    import sqlite3
    import sys
    from pathlib import Path

    if sys.platform != "darwin":
        return ProbeResult("down", "iMessage requires macOS")
    db_path = Path(
        (values.get("db_path") or "").strip() or "~/Library/Messages/chat.db"
    ).expanduser()
    if not values.get("enabled"):
        return ProbeResult("disabled", "channel disabled")
    if not db_path.exists():
        return ProbeResult("not_configured", f"Messages DB not found at {db_path}")

    def _check() -> ProbeResult:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                count = conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
            finally:
                conn.close()
            return ProbeResult("ok", f"chat.db readable · {count} conversations")
        except sqlite3.OperationalError as e:
            if "authorization denied" in str(e) or "unable to open" in str(e):
                return ProbeResult(
                    "auth_failed",
                    "needs Full Disk Access (System Settings → Privacy & Security)",
                )
            return ProbeResult("down", f"sqlite: {e}")
        except Exception as e:  # noqa: BLE001 — probes never raise
            return ProbeResult("down", f"{type(e).__name__}: {e}")

    db_result = await asyncio.to_thread(_check)

    # When BlueBubbles is configured it owns outbound — surface its reachability.
    bb_url = (values.get("bluebubbles_url") or "").strip()
    if bb_url:
        from flowly.channels._imessage_bluebubbles import health_check

        ok = await health_check(bb_url, values.get("bluebubbles_password") or "")
        bb = "BlueBubbles reachable" if ok else "BlueBubbles unreachable"
        if db_result.status == "ok":
            return ProbeResult("ok" if ok else "down", f"{db_result.detail} · {bb}")
        return ProbeResult(db_result.status, f"{db_result.detail} · {bb}")

    return db_result


async def probe_email(values: dict[str, Any]) -> ProbeResult:
    # Gmail uses OAuth credentials at ~/.flowly/gmail-credentials.json — we
    # can detect presence but proper validation needs a token refresh.
    from pathlib import Path
    creds = Path.home() / ".flowly" / "gmail-credentials.json"
    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if creds.exists() else "not_configured",
            "credentials present · channel disabled" if creds.exists() else "no OAuth credentials",
        )
    if not creds.exists():
        return ProbeResult("not_configured", "run gmail OAuth setup")
    return ProbeResult("ok", "OAuth credentials on disk")


async def probe_fal_image(values: dict[str, Any]) -> ProbeResult:
    """FAL image generation — presence check only.

    FAL exposes no free validation endpoint, so we don't burn a generation just
    to verify the key; we only report whether one is set / enabled.
    """
    has_key = bool((values.get("api_key") or "").strip())
    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if has_key else "not_configured",
            "key set · disabled" if has_key else "no API key",
        )
    return ProbeResult("ok", "key set") if has_key else ProbeResult("not_configured", "no API key")


async def probe_brave_search(values: dict[str, Any]) -> ProbeResult:
    """Brave Search — presence check (own key OR logged-in Flowly proxy).

    No network call: Brave's free key has no cheap validate endpoint and the
    proxy path is gated server-side. We report whether a usable credential
    exists — a direct ``api_key`` or the account relay creds written by
    ``flowly login``.
    """
    has_key = bool((values.get("api_key") or "").strip())
    logged_in = False
    try:
        from flowly.config.loader import load_config

        web = getattr(getattr(load_config(), "channels", None), "web", None)
        logged_in = bool(getattr(web, "server_id", "") and getattr(web, "auth_token", ""))
    except Exception:
        logged_in = False

    configured = has_key or logged_in
    detail = "own key" if has_key else ("Flowly proxy" if logged_in else "no key / not logged in")
    if not values.get("enabled", True):
        return ProbeResult("disabled", f"{detail} · disabled" if configured else "disabled")
    return ProbeResult("ok", detail) if configured else ProbeResult("not_configured", detail)


async def probe_ddgs(values: dict[str, Any]) -> ProbeResult:
    """DuckDuckGo (ddgs) — reports whether the ddgs package is installed."""
    try:
        import ddgs  # noqa: F401

        installed = True
    except ImportError:
        installed = False

    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if installed else "not_configured",
            "installed · disabled" if installed else "ddgs not installed",
        )
    if installed:
        return ProbeResult("ok", "ddgs installed")
    return ProbeResult("not_configured", "run: pip install ddgs")


async def probe_searxng(values: dict[str, Any]) -> ProbeResult:
    """SearXNG — presence check on the configured instance URL."""
    url = (values.get("url") or "").strip()
    if not values.get("enabled"):
        return ProbeResult("disabled" if url else "not_configured", "disabled" if url else "no URL")
    return ProbeResult("ok", url) if url else ProbeResult("not_configured", "no instance URL")


def _cred_probe(has_cred: bool, enabled: bool) -> ProbeResult:
    """Shared presence-only probe for key-based web search backends."""
    if not enabled:
        return ProbeResult("disabled" if has_cred else "not_configured", "key set · disabled" if has_cred else "no key")
    return ProbeResult("ok", "key set") if has_cred else ProbeResult("not_configured", "no API key")


async def probe_tavily(values: dict[str, Any]) -> ProbeResult:
    return _cred_probe(bool((values.get("api_key") or "").strip()), bool(values.get("enabled")))


async def probe_exa(values: dict[str, Any]) -> ProbeResult:
    return _cred_probe(bool((values.get("api_key") or "").strip()), bool(values.get("enabled")))


async def probe_parallel(values: dict[str, Any]) -> ProbeResult:
    return _cred_probe(bool((values.get("api_key") or "").strip()), bool(values.get("enabled")))


async def probe_firecrawl(values: dict[str, Any]) -> ProbeResult:
    has_cred = bool((values.get("api_key") or "").strip()) or bool((values.get("api_url") or "").strip())
    return _cred_probe(has_cred, bool(values.get("enabled")))


async def probe_web_channel(values: dict[str, Any]) -> ProbeResult:
    """iOS pairing / web relay — driven by /login, not the form."""
    server_id = (values.get("server_id") or "").strip()
    token = (values.get("auth_token") or "").strip()
    if values.get("enabled") and server_id and token:
        return ProbeResult("ok", f"server {server_id[:8]}…")
    if server_id or token:
        return ProbeResult("disabled", "partially configured")
    return ProbeResult("not_configured", "run /login to pair iOS")


async def probe_flowly_account(values: dict[str, Any]) -> ProbeResult:
    """Flowly hosted provider — gated on account + server registration.

    The actual auth is ``Bearer {serverId}:{gatewayAuthToken}``, so we
    need BOTH a signed-in account AND a registered server. ``id_token``
    alone (Firebase JWT) is not enough — the proxy rejects with "missing
    server identification". An account missing server_id usually means
    ``register_machine`` failed during login; the user should /logout
    and /login again to retry.
    """
    from flowly.account.auth import load_account_sync
    account = load_account_sync()
    if not values.get("enabled"):
        if account is not None:
            return ProbeResult("disabled", f"signed in as {account.email or account.user_id} · hosted off")
        return ProbeResult("not_configured", "not signed in")
    if account is None:
        return ProbeResult("not_configured", "sign in to use Flowly hosted")
    if not (account.server_id and account.gateway_auth_token):
        return ProbeResult(
            "auth_failed",
            "signed in but server not registered — /logout and /login again",
        )
    return ProbeResult("ok", account.email or account.user_id)


# ── tools ──────────────────────────────────────────────────────────


async def probe_home_assistant(values: dict[str, Any]) -> ProbeResult:
    url = (values.get("url") or "").strip().rstrip("/")
    token = (values.get("token") or "").strip()
    if not url or not token:
        return ProbeResult("not_configured", "url + token required")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{url}/api/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": _UA,
                },
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code == 401:
        return ProbeResult("auth_failed", "token rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    # Try entities count for a more useful badge.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{url}/api/states",
                headers={"Authorization": f"Bearer {token}", "User-Agent": _UA},
            )
        if r.status_code == 200:
            try:
                entities = r.json()
                return ProbeResult("ok", f"{len(entities)} entities")
            except Exception:
                pass
    except Exception:
        pass
    return ProbeResult("ok", "reachable")


async def probe_linear(values: dict[str, Any]) -> ProbeResult:
    key = (values.get("api_key") or "").strip()
    if not key:
        return ProbeResult("not_configured", "API key missing")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://api.linear.app/graphql",
                headers={
                    "Authorization": key if key.startswith("lin_api_") else f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": _UA,
                },
                json={"query": "{ viewer { id email } }"},
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code in (401, 403):
        return ProbeResult("auth_failed", "API key rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        data = r.json()
        email = data.get("data", {}).get("viewer", {}).get("email")
        if email:
            return ProbeResult("ok", email)
    except Exception:
        pass
    return ProbeResult("ok", "authenticated")


async def probe_github(values: dict[str, Any]) -> ProbeResult:
    token = (values.get("token") or "").strip()
    if not token:
        return ProbeResult("not_configured", "token missing")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": _UA,
                },
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code == 401:
        return ProbeResult("auth_failed", "token rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        login = r.json().get("login")
        if login:
            return ProbeResult("ok", f"@{login}")
    except Exception:
        pass
    return ProbeResult("ok", "authenticated")


async def probe_sentry(values: dict[str, Any]) -> ProbeResult:
    token = (values.get("token") or "").strip()
    org = (values.get("org") or "").strip()
    if not token:
        return ProbeResult("not_configured", "token missing")
    if not org:
        return ProbeResult("not_configured", "org slug missing")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"https://sentry.io/api/0/organizations/{org}/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": _UA,
                },
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code == 401:
        return ProbeResult("auth_failed", "token rejected")
    if r.status_code in (403, 404):
        return ProbeResult("auth_failed", "org not accessible")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        name = r.json().get("slug") or org
        return ProbeResult("ok", name)
    except Exception:
        pass
    return ProbeResult("ok", "authenticated")


async def probe_obsidian(values: dict[str, Any]) -> ProbeResult:
    """Check the Obsidian vault is reachable and holds at least one note.

    Purely local filesystem work — no network. Counts up to a small cap so a
    huge vault never blocks the catalog modal.
    """
    from flowly.obsidian.vault import (
        VaultNotConfigured,
        VaultPermissionDenied,
        iter_notes,
        resolve_vault_path,
    )

    if not values.get("enabled"):
        return ProbeResult("disabled", "integration disabled")
    try:
        root = resolve_vault_path(values.get("vault_path") or "")
    except VaultPermissionDenied:
        return ProbeResult("auth_failed", "permission denied — grant Full Disk Access, then restart")
    except VaultNotConfigured as exc:
        return ProbeResult("not_configured", str(exc))
    except Exception as exc:  # noqa: BLE001 — probes never raise
        return ProbeResult("down", f"{type(exc).__name__}: {exc}")

    try:
        count = 0
        cap = 500
        for _ in iter_notes(
            root,
            include_globs=values.get("include_globs") or ["**/*.md"],
            exclude_globs=values.get("exclude_globs")
            or [".obsidian/**", ".trash/**", ".git/**", "node_modules/**"],
        ):
            count += 1
            if count >= cap:
                return ProbeResult("ok", f"{cap}+ notes")
    except Exception as exc:  # noqa: BLE001
        return ProbeResult("down", f"{type(exc).__name__}: {exc}")

    if count == 0:
        return ProbeResult("not_configured", "no .md notes found in vault")
    return ProbeResult("ok", f"{count} note{'s' if count != 1 else ''}")


async def probe_trello(values: dict[str, Any]) -> ProbeResult:
    key = (values.get("api_key") or "").strip()
    token = (values.get("token") or "").strip()
    if not key or not token:
        return ProbeResult("not_configured", "api_key + token required")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                "https://api.trello.com/1/members/me",
                params={"key": key, "token": token},
                headers={"User-Agent": _UA},
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code in (401, 403):
        return ProbeResult("auth_failed", "key/token rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        data = r.json()
        return ProbeResult("ok", data.get("username") or "authenticated")
    except Exception:
        return ProbeResult("ok", "authenticated")


async def probe_x(values: dict[str, Any]) -> ProbeResult:
    bearer = (values.get("bearer_token") or "").strip()
    if not bearer:
        return ProbeResult("not_configured", "bearer token required for reads")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            # /2/users/me requires OAuth 2.0 user context, not app bearer.
            # The lightest app-bearer probe is /2/tweets/search/recent with
            # a trivial query (returns 401 if bearer invalid, 200 otherwise).
            # NOTE: the query must contain a non-stopword clause or X rejects
            # it with HTTP 400 ("Rules must contain at least one positive,
            # non-stopword clause"). "the"/"a"/"is" are stopwords — don't use them.
            r = await c.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={"query": "twitter", "max_results": 10},
                headers={"Authorization": f"Bearer {bearer}", "User-Agent": _UA},
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code in (401, 403):
        return ProbeResult("auth_failed", "bearer token rejected")
    if r.status_code == 429:
        # Auth is fine, just rate-limited — count as ok.
        return ProbeResult("ok", "rate-limited (creds ok)")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    # Detect write capability based on whether all OAuth1 fields are set.
    write = all(
        values.get(k) for k in ("api_key", "api_secret", "access_token", "access_token_secret")
    )
    return ProbeResult("ok", "read+write" if write else "read-only")


async def probe_google_workspace(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        return ProbeResult("disabled", "not enabled")
    email = (values.get("email") or "").strip()
    if not email:
        return ProbeResult("not_configured", "no connected account")
    # Token presence is the real proxy for "connected". The OAuth tokens
    # live in ~/.flowly/google-credentials.json (mirrors gmail).
    from pathlib import Path
    creds = Path.home() / ".flowly" / "google-credentials.json"
    if not creds.exists():
        return ProbeResult("auth_failed", "OAuth credentials missing — re-auth needed")
    return ProbeResult("ok", email)


# ── voice ──────────────────────────────────────────────────────────


async def probe_twilio(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled"):
        return ProbeResult(
            "disabled" if values.get("twilio_account_sid") else "not_configured",
            "credentials set · voice disabled" if values.get("twilio_account_sid") else "no credentials",
        )
    sid = (values.get("twilio_account_sid") or "").strip()
    token = (values.get("twilio_auth_token") or "").strip()
    if not sid or not token:
        return ProbeResult("not_configured", "SID + auth token required")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                headers={"Authorization": f"Basic {auth}", "User-Agent": _UA},
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code == 401:
        return ProbeResult("auth_failed", "credentials rejected")
    if r.status_code != 200:
        return ProbeResult("down", f"HTTP {r.status_code}")
    try:
        data = r.json()
        return ProbeResult("ok", data.get("friendly_name") or "authenticated")
    except Exception:
        return ProbeResult("ok", "authenticated")


# ── LLM providers ──────────────────────────────────────────────────


def _provider_probe(base: str, models_path: str = "/v1/models"):
    """Build an OpenAI-compatible probe: GET {base}{models_path} with Bearer."""
    async def probe(values: dict[str, Any]) -> ProbeResult:
        key = (values.get("api_key") or "").strip()
        if not key:
            return ProbeResult("not_configured", "API key missing")
        url = ((values.get("api_base") or base).rstrip("/")) + models_path
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(
                    url,
                    headers={"Authorization": f"Bearer {key}", "User-Agent": _UA},
                )
        except Exception as exc:
            return _net_error(exc)
        if r.status_code in (401, 403):
            return ProbeResult("auth_failed", "API key rejected")
        if r.status_code != 200:
            return ProbeResult("down", f"HTTP {r.status_code}")
        try:
            data = r.json()
            models = data.get("data") or data.get("models") or []
            return ProbeResult("ok", f"{len(models)} models")
        except Exception:
            return ProbeResult("ok", "authenticated")
    return probe


probe_openai = _provider_probe("https://api.openai.com")
probe_openrouter = _provider_probe("https://openrouter.ai/api")
probe_gemini = _provider_probe("https://generativelanguage.googleapis.com", "/v1beta/models")
probe_groq = _provider_probe("https://api.groq.com/openai")
probe_xai = _provider_probe("https://api.x.ai")
probe_zhipu = _provider_probe("https://open.bigmodel.cn/api/paas")
probe_sakana = _provider_probe("https://api.sakana.ai")


async def probe_zai_coding(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled", True):
        return ProbeResult("disabled", "GLM Coding Plan disabled")
    try:
        from flowly.auth.zai_coding import resolve_runtime_credentials
        from flowly.config.loader import load_config

        creds = resolve_runtime_credentials(config=load_config())
    except Exception as exc:
        return ProbeResult("unknown", f"credential check failed: {type(exc).__name__}")
    if creds is None or not creds.api_key:
        return ProbeResult("not_configured", "run `flowly glm login` or connect OpenCode")
    if creds.source == "opencode":
        suffix = f" ({creds.provider_id})" if creds.provider_id else ""
        return ProbeResult("ok", f"OpenCode{suffix}")
    if creds.source == "env":
        return ProbeResult("ok", "environment key")
    return ProbeResult("ok", "Flowly key store")


async def probe_xai_oauth(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled", True):
        return ProbeResult("disabled", "subscription OAuth disabled")
    try:
        from flowly.auth.xai_oauth import load_token_payload, token_is_expiring
        payload = load_token_payload()
    except Exception as exc:
        return ProbeResult("unknown", f"token check failed: {type(exc).__name__}")
    if payload is None:
        return ProbeResult("not_configured", "run `flowly xai login`")
    if token_is_expiring(payload):
        return ProbeResult("disabled", "token needs refresh")
    return ProbeResult("ok", payload.email or "OAuth connected")


async def probe_openai_codex(values: dict[str, Any]) -> ProbeResult:
    if not values.get("enabled", True):
        return ProbeResult("disabled", "ChatGPT subscription disabled")
    try:
        from flowly.auth.openai_codex import load_token_payload, token_is_expiring
        payload = load_token_payload()
    except Exception as exc:
        return ProbeResult("unknown", f"token check failed: {type(exc).__name__}")
    if payload is None:
        return ProbeResult("not_configured", "run `flowly codex login`")
    if not payload.account_id:
        return ProbeResult("not_configured", "sign in again with `flowly codex login`")
    label = payload.email or "ChatGPT connected"
    if payload.plan:
        label = f"{label} · {payload.plan}"
    if token_is_expiring(payload):
        return ProbeResult("ok", f"{label} (refreshing)")
    return ProbeResult("ok", label)


async def probe_anthropic(values: dict[str, Any]) -> ProbeResult:
    key = (values.get("api_key") or "").strip()
    if not key:
        return ProbeResult("not_configured", "API key missing")
    base = (values.get("api_base") or "https://api.anthropic.com").rstrip("/")
    # Anthropic has no public /models endpoint that works with a bare key;
    # the cheapest validation is a tiny messages call with max_tokens=1.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    "User-Agent": _UA,
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "."}],
                },
            )
    except Exception as exc:
        return _net_error(exc)
    if r.status_code in (401, 403):
        return ProbeResult("auth_failed", "API key rejected")
    # 200 OK or 400 with model-related error both mean creds are valid.
    if r.status_code == 200:
        return ProbeResult("ok", "authenticated")
    if r.status_code == 400:
        try:
            err = r.json().get("error", {}).get("type", "")
            if err and "auth" not in err:
                return ProbeResult("ok", "authenticated")
        except Exception:
            pass
    return ProbeResult("down", f"HTTP {r.status_code}")


# ── helpers ────────────────────────────────────────────────────────


async def run_with_timeout(coro, seconds: float = 6.0) -> ProbeResult:
    """Wrap a probe in an outer timeout safety net."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        return ProbeResult("down", f"timeout after {seconds:.0f}s")
    except Exception as exc:
        return ProbeResult("unknown", f"probe crashed: {type(exc).__name__}")
