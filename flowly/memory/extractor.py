"""Live extractor for the cross-session memory dreamer.

Turns a message delta (recent conversation across sessions) into `Candidate`
memories. It is **tool-less by construction** — it returns data; the dreamer
engine owns every write — so it never needs the message/exec/skill/memory tools.

Why a direct provider call rather than a full subagent: the proven-reliable path
for these structured, reasoning-model completions is the loop's already-
authenticated `provider.chat_stream` (a non-streamed CLI call hits the Flowly
proxy's 504 / empty-stream behavior — the same reason `memory_consolidate` uses
streaming + retry). Streaming + accumulate + tolerant-parse gives us the same
robustness with none of the subagent-loop overhead.

Sync/async bridge: `MemoryDreamerService.run()` is synchronous and calls
`extract()` synchronously. The dreamer is run in a worker thread
(`asyncio.to_thread`) so its SQLite writes never block the event loop; `extract()`
bridges the LLM call back to the loop with `run_coroutine_threadsafe` (the
provider lives on the loop). The worker thread blocks on the future while the
loop stays responsive.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any, Sequence

from loguru import logger

from flowly.memory.dreamer import Candidate, ExtractionError, MessageRow
from flowly.memory.governance import VALID_KINDS, VALID_PRIVACY

_EXTRACT_PROMPT = """You extract DURABLE long-term memories from a recent conversation \
(it may span several sessions). Return ONLY a JSON array — no prose, no code fence.

Extract facts worth remembering for months: the user's profile (name, role, \
location, setup), stable preferences, ongoing projects, their environment/tools, \
important relationships, recurring procedures, and explicit corrections.

Do NOT extract: one-off task details, transient state, things obvious from a single \
message, or anything you only weakly inferred. Prefer a few high-quality items over many.

USER PROFILE — already on file about the user (do NOT re-extract anything already \
stated here, e.g. their name if it appears below):
{profile}

ALREADY REMEMBERED — facts already in long-term memory (key | text):
{known}

Reconcile against the profile AND that list BEFORE returning anything:
- If a fact is already covered there, do NOT return it again (no duplicates).
- If the conversation CONTRADICTS or UPDATES one of them (a corrected location, a \
changed preference, an outdated fact), return the corrected version and REUSE THE \
EXACT key shown in [brackets] as "normalized_key" — this replaces the old fact \
instead of adding a competing one. Set "is_explicit": true when the user stated the \
change directly.
- Otherwise only return genuinely NEW facts.

Each array element MUST be an object:
{
  "kind": one of ["profile","preference","project","environment","relationship","procedure","temporal","correction"],
  "text": a concise, self-contained statement WITHOUT "the user said" framing (e.g. "Prefers pytest + ruff"),
  "normalized_key": a short stable dedup key — REUSE an existing [bracketed] key when correcting that fact, otherwise a new one like "profile:name", "pref:editor", "project:flowly-oss",
  "privacy_level": "normal" | "sensitive" | "secret"   (secret = passwords, API keys, tokens — never store the secret value itself),
  "is_explicit": true if the USER stated it directly, false if you inferred it,
  "confidence": a number 0.0-1.0
}

If there is nothing new or changed to remember, return []. Output JSON only.

Conversation:
{transcript}
"""

# Map an unknown/"fact" kind from the model onto a safe inline kind. The dreamer
# produces inline candidates (free-form); structured facts flow through the live
# knowledge_graph capture path, not here.
_KIND_FALLBACK = "preference"


class SubagentExtractor:
    """`Extractor` implementation backed by the loop's streaming provider."""

    def __init__(
        self,
        *,
        provider: Any,
        model: str,
        loop: asyncio.AbstractEventLoop | None = None,
        max_transcript_chars: int = 24_000,
        timeout_s: float = 180.0,
    ):
        self._provider = provider
        self._model = model
        # When a loop is given (live agent), the dreamer runs in a worker thread
        # and the LLM call is bridged back to that loop (the provider lives on it).
        # When it is None (standalone RPC — the "Learn from chats" action), the
        # whole pass already runs off the event loop, so we drive the coroutine
        # directly with asyncio.run, exactly like memory_consolidate's _propose.
        self._loop = loop
        self._max_chars = max_transcript_chars
        self._timeout = timeout_s

    # -- Extractor protocol -------------------------------------------------

    def extract(
        self, delta: Sequence[MessageRow], known: Sequence[Any] = (), profile: str = ""
    ) -> list[Candidate]:
        """Sync entry (called by the dreamer in a worker thread). Bridges the
        async LLM call to the event loop and parses the result.

        ``known`` is the existing active/queued memory (MemoryItem-like, with
        ``normalized_key`` + ``text``). It is injected into the prompt so the
        model deduplicates against what's already remembered and reuses an
        existing key when correcting it — which lets the engine supersede the
        old fact instead of accumulating a contradicting duplicate.

        ``profile`` is the user's curated profile text (USER.md), injected as
        dedup-only context so the dreamer doesn't re-propose facts already on
        file there (e.g. the user's name). It is not key-reconciled — the profile
        is user-owned and the dreamer never rewrites it."""
        if not delta:
            return []
        try:
            if self._loop is not None:
                fut = asyncio.run_coroutine_threadsafe(
                    self._extract_async(delta, known, profile), self._loop
                )
                raw = fut.result(timeout=self._timeout)
            else:
                raw = asyncio.run(self._extract_async(delta, known, profile))
        except Exception as exc:  # LLM bridge / timeout — an infra failure, not "nothing"
            raise ExtractionError(f"LLM bridge failed: {exc}") from exc
        if not raw.strip():
            # Empty after all retries → the model never produced output. Treat as
            # an infra failure so the engine holds the watermark and retries,
            # rather than silently skipping this delta forever. A genuine "nothing
            # to learn" comes back as a parseable ``[]`` and returns cleanly below.
            raise ExtractionError("empty extractor response after retries")
        return self._parse(raw, delta)

    # -- LLM call (on the loop) ---------------------------------------------

    async def _extract_async(
        self, delta: Sequence[MessageRow], known: Sequence[Any] = (), profile: str = ""
    ) -> str:
        prompt = self._build_prompt(delta, known, profile)
        raw = ""
        for attempt in range(3):
            parts: list[str] = []
            try:
                async for d in self._provider.chat_stream(
                    [{"role": "user", "content": prompt}],
                    model=self._model,
                    max_tokens=2048,
                    temperature=0.2,
                ):
                    if getattr(d, "content", None):
                        parts.append(d.content)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dreamer-extract] attempt {attempt + 1} failed: {exc}")
                continue
            raw = "".join(parts)
            if raw.strip():
                break
            logger.warning(f"[dreamer-extract] attempt {attempt + 1} empty; retrying")
        return raw

    # -- pure, testable helpers --------------------------------------------

    def _build_prompt(
        self, delta: Sequence[MessageRow], known: Sequence[Any] = (), profile: str = ""
    ) -> str:
        # Render most-recent-first within the char budget, then restore order.
        rendered: list[str] = []
        used = 0
        for m in reversed(list(delta)):
            role = (m.role or "?").strip()
            content = " ".join((m.content or "").split())
            line = f"[{m.session_key}] {role}: {content}"
            if used + len(line) > self._max_chars and rendered:
                break
            rendered.append(line)
            used += len(line) + 1
        transcript = "\n".join(reversed(rendered))
        return (
            _EXTRACT_PROMPT
            .replace("{profile}", _render_profile(profile))
            .replace("{known}", _render_known(known))
            .replace("{transcript}", transcript)
        )

    def _parse(self, raw: str, delta: Sequence[MessageRow]) -> list[Candidate]:
        arr = _extract_json_array(raw)
        if not arr:
            return []
        source_session, source_ids = _provenance(delta)
        out: list[Candidate] = []
        for obj in arr:
            cand = _to_candidate(obj, source_session, source_ids)
            if cand is not None:
                out.append(cand)
        return out


# ── module-level helpers (unit-tested directly) ──────────────────────────────


def _render_profile(profile: str, max_chars: int = 4000) -> str:
    """The user's curated profile (USER.md) as dedup context, trimmed/capped."""
    text = (profile or "").strip()
    if not text:
        return "(no profile on file)"
    return text[:max_chars]


def _render_known(known: Sequence[Any], max_chars: int = 4000) -> str:
    """Render existing memory as ``- [key] text`` lines for the prompt, capped so
    a large store can't blow the token budget. Items are taken in the order given
    (the dreamer passes most-recently-updated first)."""
    rows: list[str] = []
    used = 0
    for it in known or ():
        key = (getattr(it, "normalized_key", "") or "").strip() or "no-key"
        text = " ".join((getattr(it, "text", "") or "").split())
        if not text:
            continue
        line = f"- [{key}] {text}"
        if used + len(line) > max_chars and rows:
            break
        rows.append(line)
        used += len(line) + 1
    return "\n".join(rows) if rows else "(nothing remembered yet)"


def _extract_json_array(raw: str) -> list[dict]:
    """Pull a JSON array of objects out of a (possibly fenced/prefixed) string."""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    # Strip a ```json … ``` fence if present.
    if text.startswith("```"):
        text = text.split("```", 2)
        text = text[1] if len(text) > 1 else ""
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    # Narrow to the first [...] span.
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _provenance(delta: Sequence[MessageRow]) -> tuple[str, list[str]]:
    if not delta:
        return "", []
    session = Counter(m.session_key for m in delta).most_common(1)[0][0]
    ids = sorted({m.id for m in delta})
    # Cap the provenance id list; it's a forensic pointer, not load-bearing.
    if len(ids) > 20:
        ids = [ids[0], ids[-1]]
    return session, [str(i) for i in ids]


def _to_candidate(
    obj: dict, source_session: str, source_ids: list[str]
) -> Candidate | None:
    text = str(obj.get("text") or "").strip()
    if not text:
        return None
    kind = str(obj.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS or kind == "fact":
        kind = _KIND_FALLBACK
    privacy = str(obj.get("privacy_level") or "normal").strip().lower()
    if privacy not in VALID_PRIVACY:
        privacy = "normal"
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    key = str(obj.get("normalized_key") or "").strip().lower()
    return Candidate(
        kind=kind,
        text=text,
        normalized_key=key,
        ref_kind="inline",
        ref_id=None,
        privacy_level=privacy,
        confidence=confidence,
        source_session=source_session,
        source_message_ids=list(source_ids),
        is_explicit=bool(obj.get("is_explicit", False)),
    )
