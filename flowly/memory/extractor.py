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

from flowly.memory.dreamer import Candidate, MessageRow
from flowly.memory.governance import VALID_KINDS, VALID_PRIVACY

_EXTRACT_PROMPT = """You extract DURABLE long-term memories from a recent conversation \
(it may span several sessions). Return ONLY a JSON array — no prose, no code fence.

Extract facts worth remembering for months: the user's profile (name, role, \
location, setup), stable preferences, ongoing projects, their environment/tools, \
important relationships, recurring procedures, and explicit corrections.

Do NOT extract: one-off task details, transient state, things obvious from a single \
message, or anything you only weakly inferred. Prefer a few high-quality items over many.

Each array element MUST be an object:
{
  "kind": one of ["profile","preference","project","environment","relationship","procedure","temporal","correction"],
  "text": a concise, self-contained statement WITHOUT "the user said" framing (e.g. "Prefers pytest + ruff"),
  "normalized_key": a short stable dedup key, e.g. "profile:name", "pref:editor", "project:flowly-oss",
  "privacy_level": "normal" | "sensitive" | "secret"   (secret = passwords, API keys, tokens — never store the secret value itself),
  "is_explicit": true if the USER stated it directly, false if you inferred it,
  "confidence": a number 0.0-1.0
}

If there is nothing durable to remember, return []. Output JSON only.

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
        loop: asyncio.AbstractEventLoop,
        max_transcript_chars: int = 24_000,
        timeout_s: float = 180.0,
    ):
        self._provider = provider
        self._model = model
        self._loop = loop
        self._max_chars = max_transcript_chars
        self._timeout = timeout_s

    # -- Extractor protocol -------------------------------------------------

    def extract(self, delta: Sequence[MessageRow]) -> list[Candidate]:
        """Sync entry (called by the dreamer in a worker thread). Bridges the
        async LLM call to the event loop and parses the result."""
        if not delta:
            return []
        try:
            fut = asyncio.run_coroutine_threadsafe(self._extract_async(delta), self._loop)
            raw = fut.result(timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — extraction must never crash a run
            logger.warning(f"[dreamer-extract] LLM bridge failed: {exc}")
            return []
        return self._parse(raw, delta)

    # -- LLM call (on the loop) ---------------------------------------------

    async def _extract_async(self, delta: Sequence[MessageRow]) -> str:
        prompt = self._build_prompt(delta)
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

    def _build_prompt(self, delta: Sequence[MessageRow]) -> str:
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
        return _EXTRACT_PROMPT.replace("{transcript}", transcript)

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
