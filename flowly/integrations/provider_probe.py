"""Ask the configured provider one real question, and report what happened.

Setup used to end on "✓ key saved" — a claim about a *file*, not about the
provider. A truncated paste, an exhausted balance, a plan that doesn't cover
the chosen model, a region block: none of that is visible until the user's
first message, by which point the failure looks like a broken agent rather
than an unfinished setup.

So the last step of onboarding sends one cheap request and reports the
verdict. Cheap on purpose — a handful of output tokens, a short timeout — and
never fatal: a probe that can't reach the network says so and leaves the
config exactly as it was.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Short enough that a hung provider doesn't hold the terminal, long enough for
# a cold serverless endpoint to answer.
PROBE_TIMEOUT_SECONDS = 25.0

_PROMPT = "Reply with the single word: ready"

# Enough room that a reasoning model can spend its thinking budget and still
# emit something. The probe judges "did the call go through", not the answer,
# so this stays a rounding error on any plan.
_PROBE_MAX_TOKENS = 32


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    seconds: float = 0.0
    model: str = ""

    @property
    def summary(self) -> str:
        if self.ok:
            return f"answered in {self.seconds:.1f}s"
        return self.detail


def _classify(exc: Exception) -> str:
    """Turn a provider exception into one line a person can act on."""
    if isinstance(exc, asyncio.TimeoutError):
        return f"no answer within {PROBE_TIMEOUT_SECONDS:.0f}s — network or provider is slow"
    return _classify_text(str(exc))


def _classify_text(text: str) -> str:
    """Same mapping for providers that report failure as response text."""
    text = text.strip()
    low = text.lower()
    if "401" in low or "unauthor" in low or "invalid api key" in low or "invalid_api_key" in low:
        return "the provider rejected this key (401) — check it was pasted whole"
    if "403" in low or "forbidden" in low:
        return "the key is valid but not allowed here (403) — plan or region"
    if "429" in low or "rate limit" in low:
        return "rate-limited right now (429) — the key works, try again shortly"
    if "402" in low or "quota" in low or "credit" in low or "billing" in low:
        return "no credit/quota left on this account"
    if "404" in low and "model" in low:
        return "this provider doesn't serve the configured model"
    if "timeout" in low or "timed out" in low:
        return f"no answer within {PROBE_TIMEOUT_SECONDS:.0f}s — network or provider is slow"
    if "name or service not known" in low or "temporary failure" in low or "connection" in low:
        return "couldn't reach the provider — check your network"
    return text[:160] or "the provider reported an unspecified error"


async def _probe_async(config) -> ProbeResult:
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.providers.factory import build_provider

    active = resolve_active_provider(config)
    if active is None:
        return ProbeResult(False, "no provider is configured")

    model = (config.agents.defaults.model or "").strip()
    try:
        provider = build_provider(active, default_model=model, config=config)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, _classify(exc), model=model)

    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            provider.chat(
                messages=[{"role": "user", "content": _PROMPT}],
                max_tokens=_PROBE_MAX_TOKENS,
                temperature=0.0,
            ),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is a report, not a crash
        logger.debug("provider probe failed: %s", exc)
        return ProbeResult(False, _classify(exc), time.monotonic() - started, model)

    elapsed = time.monotonic() - started

    # Providers don't agree on how a failed call comes back. Several (the
    # OpenAI-compatible ones) swallow the exception and hand back a normal
    # response whose finish_reason is "error" and whose content is the message
    # — a probe that only catches exceptions would call a 401 a success, which
    # is worse than not probing at all.
    if getattr(response, "finish_reason", "") == "error":
        return ProbeResult(False, _classify_text(str(response.content or "")), elapsed, model)

    # Anything else counts as success. The question this answers is "will the
    # provider serve a request with these credentials" — not "did it say the
    # right word". A reasoning model that spends the whole budget thinking
    # comes back with empty content and finish_reason="length"; the credential
    # is fine, and failing setup over it would be a lie in the other direction.
    return ProbeResult(True, "ok", elapsed, model)


def probe_active_provider(config=None) -> ProbeResult:
    """Send one small request through the configured provider. Never raises."""
    try:
        if config is None:
            from flowly.config.loader import load_config

            config = load_config()
        return asyncio.run(_probe_async(config))
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider probe could not run: %s", exc)
        return ProbeResult(False, _classify(exc))
