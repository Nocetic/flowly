"""CoachingManager — orchestrates real-time meeting coaching.

STT happens client-side (desktop app → web app `/api/stt/transcribe`).
We only receive transcribed text here, so no STT provider dependency.

Flow per segment:
  1. Desktop sends already-transcribed text
  2. Append to rolling buffer (last N segments)
  3. Every K new segments (and not rate-limited) → gate pipeline
  4. If a tip passes the gate → dispatch to per-session callbacks

At stop:
  - Summarize transcript (best-effort)
  - Extract entities → KG (best-effort)
  - Append summary to MEMORY.md (best-effort)
  - Save full transcript as artifact (best-effort)

Enterprise guarantees:
  - Per-session callback registration (no cross-session leakage)
  - Hard cap on concurrent sessions + per-session duration
  - Background finalization so stop() returns quickly
  - All post-processing steps isolated with try/except — partial success allowed
  - Instrumented with counters and latency metrics
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from ..providers.base import LLMProvider
from . import gate as gate_pipeline

# ── Tuning constants ──────────────────────────────────────────────────────────

MAX_BUFFER_SEGMENTS = 50
MAX_TIPS_PER_SESSION = 40
MAX_CONCURRENT_SESSIONS = 5
MAX_SESSION_SECONDS = 4 * 3600    # 4 hours — hard cap per meeting
MAX_TEXT_LEN = 4000               # per-segment defensive cap
MAX_TIP_LEN = 180                 # post-gate safety clamp
KG_SUMMARY_MAX_CHARS = 2500
# Cap on how many MEMORY.md characters we feed back to the gate. Taking
# the tail (most recent meetings) is the right priority — older summaries
# are usually present in KG anyway via entity extraction.
MEMORY_TAIL_MAX_CHARS = 3000
MIN_WORDS_FOR_EVAL = 8            # skip gate if barely any content

# Silence handling: long quiet stretches reset the buffer so the next stretch
# of conversation isn't scored against stale context.
SILENCE_RESET_SECONDS = 120       # 2 min inactivity → buffer cleared
MIN_WORDS_AFTER_SILENCE = 5       # require real content before re-arming

# Frequency-driven knobs (the UI exposes this as a single slider)
#   segments      : min new segments since last eval before firing
#   seconds       : min seconds of speech accumulated since last eval before
#                   firing (whichever comes first — fast talkers hit count,
#                   slow meetings hit the clock)
#   rate_limit_s  : min gap between emitted tips
#
# Daily cap was removed in 2026-05: it produced a poor UX for users
# with several meetings on the same day (one heavy morning meeting
# could exhaust the cap and leave the afternoon meeting silent), and
# the authoritative cost / quota control already lives in the backend
# coaching rate-limit bucket (`lib/ratelimit/coaching-bucket.ts`). The
# bot's local rate_limit_s + gate1 rejection + recent-tips dedup carry
# the tip-spam defence load that the cap used to provide.
FREQUENCY_PROFILES: dict[str, dict[str, int]] = {
    "selective": {"segments": 5, "seconds": 45, "rate_limit_s": 180},
    "moderate":  {"segments": 3, "seconds": 20, "rate_limit_s": 45},
    "proactive": {"segments": 2, "seconds": 12, "rate_limit_s": 10},
}

# STT metadata artefacts (Scribe etc. label silence/noise as "[music]" etc.).
# Bracketed tags with no real speech content — discard them entirely.
_STT_NOISE_PREFIXES = (
    "[müzik", "[music", "[arka", "[background",
    "[fon", "[noise", "[sessiz", "[silence",
    "[applause", "[alkış", "[gülme", "[laughter",
    "[inaudible", "[anlaşılmaz", "[ses ", "[uğultu",
)


def _is_stt_noise(text: str) -> bool:
    """Return True if a transcript is pure STT noise metadata, not real speech."""
    t = text.strip().lower()
    if not t:
        return True
    # Pure bracketed tag: [müzik], [music], [fon müziği], [arka plan müziği]
    if t.startswith("[") and t.endswith("]"):
        return True
    for prefix in _STT_NOISE_PREFIXES:
        if t.startswith(prefix):
            return True
    return False


def _truncate_tip(text: str, limit: int) -> tuple[str, bool]:
    """Trim ``text`` to ``limit`` chars, preferring a sentence/word boundary.

    Returns (truncated_text, was_truncated).
    """
    t = text.strip()
    if len(t) <= limit:
        return t, False
    # Prefer the last sentence terminator inside the budget
    head = t[:limit]
    for terminator in (". ", "? ", "! ", "; "):
        idx = head.rfind(terminator)
        if idx >= int(limit * 0.5):  # only if we're not throwing too much away
            return head[: idx + 1].strip(), True
    # Otherwise fall back to word boundary
    space = head.rfind(" ")
    if space >= int(limit * 0.6):
        return head[:space].rstrip(",;:—-") + "…", True
    # Worst case: hard cut
    return head.rstrip() + "…", True


# ── Types ─────────────────────────────────────────────────────────────────────


@dataclass
class Segment:
    text: str
    source: str                   # "mic" | "system"
    timestamp: float
    seq: int = 0                  # monotonic per session, for renderer dedup
    speaker: str = ""             # future: speaker id


@dataclass
class SessionMetrics:
    """Per-session observability counters and timings."""
    segments_received: int = 0
    segments_accepted: int = 0
    segments_dropped_noise: int = 0
    segments_dropped_duplicate: int = 0
    segments_dropped_silence_pending: int = 0
    silence_resets: int = 0
    gate_evaluations: int = 0
    gate1_passes: int = 0
    gate2_passes: int = 0
    critic_rejects: int = 0
    tips_emitted: int = 0
    tips_blocked_rate_limit: int = 0
    tips_truncated: int = 0
    callback_failures: int = 0
    gate_latency_total_s: float = 0.0
    last_activity_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        avg_gate_latency = (
            self.gate_latency_total_s / self.gate_evaluations
            if self.gate_evaluations > 0 else 0.0
        )
        return {
            "segments_received": self.segments_received,
            "segments_accepted": self.segments_accepted,
            "segments_dropped_noise": self.segments_dropped_noise,
            "segments_dropped_duplicate": self.segments_dropped_duplicate,
            "segments_dropped_silence_pending": self.segments_dropped_silence_pending,
            "silence_resets": self.silence_resets,
            "gate_evaluations": self.gate_evaluations,
            "gate1_passes": self.gate1_passes,
            "gate2_passes": self.gate2_passes,
            "critic_rejects": self.critic_rejects,
            "tips_emitted": self.tips_emitted,
            "tips_blocked_rate_limit": self.tips_blocked_rate_limit,
            "tips_truncated": self.tips_truncated,
            "callback_failures": self.callback_failures,
            "avg_gate_latency_s": round(avg_gate_latency, 3),
        }


@dataclass
class TipRecord:
    """A tip that was emitted to the user. Buffered for snapshot/replay."""
    text: str
    confidence: float
    timestamp: float
    seq: int


@dataclass
class CoachingSession:
    """Per-session state. One session = one meeting."""
    session_id: str
    started_at: float = field(default_factory=time.time)
    user_context: str = ""
    language: str = "auto"
    frequency: str = "moderate"
    gate_mode: str | None = None  # override manager default when set
    segments: list[Segment] = field(default_factory=list)
    new_since_eval: int = 0
    last_eval_at: float = field(default_factory=time.time)  # for time-based trigger
    last_tip_at: float = 0.0
    last_activity_at: float = field(default_factory=time.time)  # silence tracking
    tips_sent: int = 0
    # Tip history for snapshot/replay. The renderer needs the actual tip
    # text to rehydrate after page navigation; just the count isn't enough.
    # Bounded to MAX_TIPS_PER_SESSION since tips are rate-limited anyway.
    tips: list[TipRecord] = field(default_factory=list)
    # Monotonic counter shared by transcript and tip events. Renderers
    # ingest both via snapshot AND live stream during the rehydrate
    # handshake; `seq` lets them deduplicate.
    next_seq: int = 0
    # Silence handling — after a long quiet stretch we require a minimum
    # amount of fresh content before re-arming the gate.
    silence_pending: bool = False
    words_after_silence: int = 0
    eval_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    # Callbacks registered per session (no cross-session leakage)
    tip_callbacks: list["TipCallback"] = field(default_factory=list)
    transcript_callbacks: list["TranscriptCallback"] = field(default_factory=list)
    finalized_callbacks: list["FinalizedCallback"] = field(default_factory=list)
    gate_decision_callbacks: list["GateDecisionCallback"] = field(default_factory=list)
    # Last few texts for dedup (STT sometimes repeats)
    _recent_texts: list[str] = field(default_factory=list)
    # Faz E screen-capture: the latest base64 JPEG sent by the desktop
    # with a transcript segment. Single-slot — newest overwrites older
    # because gates only ever consume the most-recent visual context.
    # Consumed and cleared inside _maybe_evaluate so the same frame
    # never feeds two gate calls. ``None`` is the resting state when
    # the desktop hasn't sent an image (or has sent text-only commits).
    latest_screenshot_b64: str | None = None
    # Hotkey-triggered "ask now" cooldown. Prevents the user from spam-
    # firing the global hotkey (both Cmds held) and racking up gate2
    # calls — a single accidental flutter of fingers can otherwise
    # dispatch 5 calls in under a second.
    last_ask_now_at: float = 0.0


TipCallback = Callable[..., Awaitable[None]]
"""Called when a tip passes all gates.
Args (positional): session_id, tip_text, confidence.
Optional kwargs: seq (int) — monotonic event sequence for client dedup,
                 timestamp (float)."""

TranscriptCallback = Callable[..., Awaitable[None]]
"""Called for every transcribed segment.
Args (positional): session_id, text, source.
Optional kwargs: seq (int) — monotonic event sequence for client dedup."""

FinalizedCallback = Callable[[str, dict], Awaitable[None]]
"""Called when background finalization finishes. Args: session_id, summary_dict."""

GateDecisionCallback = Callable[..., Awaitable[None]]
"""Called for every gate decision (pass or reject) so clients can render
diagnostics. Required positional: session_id, stage. Required keyword:
passed (bool). Optional keywords: score (float|None), reason (str),
threshold (float|None), latency_ms (float|None), extras (dict).

Stages:
  - 'gate1' — relevance gate (LLM score vs frequency threshold)
  - 'rate_limit' — gate1 passed but rate-limit blocked emission
  - 'gate2' — tip text generation (passed = produced non-empty tip)
  - 'critic' — final usefulness check (only when use_critic=True)
  - 'emit' — tip actually delivered to user
"""


# ── Manager ───────────────────────────────────────────────────────────────────


class CoachingManager:
    """Manages active coaching sessions.

    One manager instance can serve multiple concurrent sessions.
    All external state (KG, memory, artifacts) is optional — the manager
    degrades gracefully if any dependency is missing.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        gate_model: str = "openrouter/anthropic/claude-haiku-4.5",
        summary_model: str = "openrouter/anthropic/claude-haiku-4.5",
        knowledge_graph: Any | None = None,
        memory_path: Path | None = None,
        artifact_store: Any | None = None,
        use_critic: bool = False,
        gate_mode: str = "assistant",
        max_concurrent_sessions: int = MAX_CONCURRENT_SESSIONS,
        max_session_seconds: int = MAX_SESSION_SECONDS,
    ):
        self.llm = llm_provider
        self.gate_model = gate_model
        self.summary_model = summary_model
        self.knowledge_graph = knowledge_graph
        self.memory_path = memory_path
        self.artifact_store = artifact_store
        self.use_critic = use_critic
        self.gate_mode = gate_mode if gate_mode in gate_pipeline.GATE_MODES else "assistant"
        self.max_concurrent_sessions = max(1, int(max_concurrent_sessions))
        self.max_session_seconds = max(60, int(max_session_seconds))

        self._sessions: dict[str, CoachingSession] = {}
        # Background finalization tasks — kept alive so they aren't GC'd
        self._pending_tasks: set[asyncio.Task] = set()
        # Global watchdog — auto-stops stale sessions
        self._watchdog_task: asyncio.Task | None = None

    # ── Callback wiring ───────────────────────────────────────────────────────

    def on_tip(self, session_id: str, callback: TipCallback) -> None:
        """Register a per-session tip handler. Multiple handlers allowed."""
        session = self._sessions.get(session_id)
        if session and callback not in session.tip_callbacks:
            session.tip_callbacks.append(callback)

    def on_transcript(self, session_id: str, callback: TranscriptCallback) -> None:
        session = self._sessions.get(session_id)
        if session and callback not in session.transcript_callbacks:
            session.transcript_callbacks.append(callback)

    def on_finalized(self, session_id: str, callback: FinalizedCallback) -> None:
        session = self._sessions.get(session_id)
        if session and callback not in session.finalized_callbacks:
            session.finalized_callbacks.append(callback)

    def on_gate_decision(
        self, session_id: str, callback: GateDecisionCallback,
    ) -> None:
        """Register a callback for gate decisions (gate1/gate2/emit/...).

        Used by the desktop renderer to populate its Diagnostics panel so
        users can see why a tip did/didn't fire. Multiple handlers allowed.
        """
        session = self._sessions.get(session_id)
        if session and callback not in session.gate_decision_callbacks:
            session.gate_decision_callbacks.append(callback)

    async def _emit_gate_decision(
        self,
        session: CoachingSession,
        stage: str,
        passed: bool,
        *,
        score: float | None = None,
        reason: str = "",
        threshold: float | None = None,
        latency_ms: float | None = None,
        extras: dict | None = None,
    ) -> None:
        """Fire registered gate-decision callbacks. Best-effort; a failing
        callback never breaks the gate pipeline."""
        if not session.gate_decision_callbacks:
            return
        payload: dict = {
            "stage": stage,
            "passed": passed,
            "reason": reason,
        }
        if score is not None:
            payload["score"] = round(score, 3)
        if threshold is not None:
            payload["threshold"] = round(threshold, 3)
        if latency_ms is not None:
            payload["latency_ms"] = round(latency_ms, 1)
        if extras:
            payload["extras"] = extras
        for cb in list(session.gate_decision_callbacks):
            try:
                await cb(session.session_id, **payload)
            except Exception as e:
                # Swallow — diagnostics must never break the gate.
                logger.debug(f"[Coach] gate_decision_cb failed: {e}")

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def start(
        self,
        session_id: str,
        user_context: str = "",
        language: str = "auto",
        frequency: str = "moderate",
    ) -> dict:
        """Begin (or reconfigure) a coaching session.

        Returns {"status": "started" | "reconfigured" | "at_capacity",
                 "started_at": float, ...}.
        """
        if not session_id or not isinstance(session_id, str):
            return {"status": "error", "error": "session_id required"}

        if frequency not in gate_pipeline.FREQUENCY_THRESHOLDS:
            frequency = "moderate"

        if session_id in self._sessions:
            existing = self._sessions[session_id]
            existing.user_context = user_context[:2000]
            existing.language = language
            existing.frequency = frequency
            logger.info(f"[Coach] session '{session_id}' reconfigured")
            return {
                "status": "reconfigured",
                "started_at": existing.started_at,
                "frequency": frequency,
            }

        if len(self._sessions) >= self.max_concurrent_sessions:
            logger.warning(
                f"[Coach] rejecting '{session_id}': at capacity "
                f"({self.max_concurrent_sessions} active)"
            )
            return {
                "status": "at_capacity",
                "active_sessions": len(self._sessions),
                "limit": self.max_concurrent_sessions,
            }

        session = CoachingSession(
            session_id=session_id,
            user_context=user_context[:2000],
            language=language,
            frequency=frequency,
        )
        session.last_eval_at = session.started_at
        self._sessions[session_id] = session
        self._ensure_watchdog()
        logger.info(
            f"[Coach] session '{session_id}' started "
            f"(freq={frequency}, lang={language}, "
            f"active={len(self._sessions)})"
        )
        return {
            "status": "started",
            "started_at": session.started_at,
            "frequency": frequency,
        }

    async def stop(self, session_id: str, background_finalize: bool = True) -> dict:
        """End a session and run post-processing.

        When ``background_finalize`` is True (default), heavy work (summary,
        KG write, artifact save) runs in a detached task so the caller gets
        a quick response. Finalization result is delivered via on_finalized.
        """
        session = self._sessions.pop(session_id, None)
        if not session:
            return {"status": "not_found"}

        duration = time.time() - session.started_at
        segment_count = len(session.segments)
        logger.info(
            f"[Coach] session '{session_id}' stopping — "
            f"{segment_count} segments, {session.tips_sent} tips, "
            f"{duration:.0f}s"
        )

        base_result: dict[str, Any] = {
            "status": "stopped",
            "duration": duration,
            "segments": segment_count,
            "tips_sent": session.tips_sent,
            "metrics": session.metrics.as_dict(),
        }

        if segment_count == 0:
            return base_result

        if background_finalize:
            # Detach finalization; caller gets immediate acknowledgement
            base_result["finalization"] = "pending"
            task = asyncio.create_task(self._finalize(session))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            return base_result

        # Synchronous fallback
        final = await self._finalize(session)
        base_result.update(final)
        return base_result

    def is_active(self, session_id: str) -> bool:
        return session_id in self._sessions

    def session_info(self, session_id: str) -> dict | None:
        s = self._sessions.get(session_id)
        if not s:
            return None
        return {
            "session_id": s.session_id,
            "started_at": s.started_at,
            "duration_s": round(time.time() - s.started_at, 1),
            "frequency": s.frequency,
            "language": s.language,
            "segments": len(s.segments),
            "tips_sent": s.tips_sent,
            "metrics": s.metrics.as_dict(),
        }

    def session_snapshot(self, session_id: str) -> dict | None:
        """Full session snapshot for client rehydration.

        Unlike `session_info` (counts only), returns the actual transcript
        segments and tip records buffered in memory. Used by clients that
        need to rebuild a live UI after a transient disconnect or page
        navigation. Bounded by MAX_BUFFER_SEGMENTS / MAX_TIPS_PER_SESSION;
        older content was already evicted from memory.
        """
        s = self._sessions.get(session_id)
        if not s:
            return None
        return {
            "session_id": s.session_id,
            "started_at": s.started_at,
            "duration_s": round(time.time() - s.started_at, 1),
            "frequency": s.frequency,
            "language": s.language,
            "status": "active",
            "next_seq": s.next_seq,
            "transcript": [
                {
                    "text": seg.text,
                    "source": seg.source,
                    "timestamp": seg.timestamp,
                    "seq": seg.seq,
                }
                for seg in s.segments
            ],
            "tips": [
                {
                    "text": t.text,
                    "confidence": t.confidence,
                    "timestamp": t.timestamp,
                    "seq": t.seq,
                }
                for t in s.tips
            ],
            "metrics": s.metrics.as_dict(),
        }

    def list_sessions(self) -> list[dict]:
        return [
            self.session_info(sid) or {"session_id": sid}
            for sid in list(self._sessions.keys())
        ]

    async def update_session(
        self,
        session_id: str,
        *,
        user_context: str | None = None,
        frequency: str | None = None,
        language: str | None = None,
    ) -> dict:
        """Live-reload session settings without stopping the meeting."""
        session = self._sessions.get(session_id)
        if not session:
            return {"status": "not_found"}
        if user_context is not None:
            session.user_context = user_context[:2000]
        if frequency is not None and frequency in gate_pipeline.FREQUENCY_THRESHOLDS:
            session.frequency = frequency
        if language is not None:
            session.language = language
        return {
            "status": "updated",
            "frequency": session.frequency,
            "language": session.language,
        }

    # ── Transcript ingestion ──────────────────────────────────────────────────

    async def add_transcript(
        self,
        session_id: str,
        text: str,
        source: str = "mic",
        screenshot_b64: str | None = None,
    ) -> dict:
        """Accept one transcribed speech segment.

        The desktop app transcribes audio via the web-app STT endpoint and
        only sends plain text here. This keeps the gateway STT-free.

        ``screenshot_b64`` is an optional base64 JPEG of the user's
        current screen, sent alongside the commit by the desktop's
        screen-capture pipeline. Stored in the session's single-slot
        cache and consumed by the NEXT gate evaluation; cleared after
        use so the same frame never feeds two gates.
        """
        session = self._sessions.get(session_id)
        if not session:
            return {"type": "error", "error": "session_not_started"}

        now = time.time()
        session.metrics.segments_received += 1

        # Latest-wins screenshot cache. We deliberately overwrite even
        # when the new commit has no image attached — a stale frame
        # from 30s ago shouldn't survive a fresh commit that the
        # desktop chose to send text-only (the desktop's smart-trigger
        # decided the screen wasn't worth re-capturing).
        if screenshot_b64 is not None:
            session.latest_screenshot_b64 = screenshot_b64

        # ── Silence handling ──────────────────────────────────────────────
        # Long quiet → the previous context is stale. Reset buffer so the
        # next stretch of conversation stands on its own. Keep the gate
        # silent until MIN_WORDS_AFTER_SILENCE words have arrived.
        quiet_for = now - session.last_activity_at
        if quiet_for > SILENCE_RESET_SECONDS and session.segments:
            logger.info(
                f"[Coach] silence reset session={session.session_id} "
                f"quiet_for={quiet_for:.0f}s buffer_was={len(session.segments)}"
            )
            session.segments.clear()
            session.new_since_eval = 0
            session.last_eval_at = now
            session._recent_texts.clear()
            session.silence_pending = True
            session.words_after_silence = 0
            session.metrics.silence_resets += 1
        session.last_activity_at = now
        session.metrics.last_activity_at = now

        if source not in ("mic", "system"):
            source = "mic"

        text = (text or "").strip()
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN]
        if not text:
            return {"type": "silence"}
        if _is_stt_noise(text):
            session.metrics.segments_dropped_noise += 1
            logger.debug(f"[Coach] dropping STT noise: {text!r}")
            return {"type": "silence", "reason": "stt_noise"}

        # Dedup: STT sometimes repeats the same phrase across adjacent flushes
        normalized = text.lower()
        if normalized in session._recent_texts:
            session.metrics.segments_dropped_duplicate += 1
            logger.debug(f"[Coach] dropping duplicate: {text!r}")
            return {"type": "silence", "reason": "duplicate"}
        session._recent_texts.append(normalized)
        if len(session._recent_texts) > 5:
            session._recent_texts.pop(0)

        # If we're in the "post-silence warm-up" phase, count words toward
        # re-arming but don't trigger the gate yet.
        if session.silence_pending:
            session.words_after_silence += len(text.split())
            if session.words_after_silence >= MIN_WORDS_AFTER_SILENCE:
                session.silence_pending = False
                session.words_after_silence = 0
                logger.debug(
                    f"[Coach] silence re-armed session={session.session_id}"
                )

        seq = session.next_seq
        session.next_seq += 1
        segment = Segment(text=text, source=source, timestamp=now, seq=seq)
        session.segments.append(segment)
        if len(session.segments) > MAX_BUFFER_SEGMENTS:
            session.segments.pop(0)
        session.new_since_eval += 1
        session.metrics.segments_accepted += 1

        profile = FREQUENCY_PROFILES.get(session.frequency, FREQUENCY_PROFILES["moderate"])
        seconds_since_eval = time.time() - session.last_eval_at
        logger.info(
            f"[Coach] segment #{len(session.segments)} src={source} "
            f"new={session.new_since_eval}/{profile['segments']} "
            f"{seconds_since_eval:.0f}s/{profile['seconds']}s "
            f"text={text[:80]!r}"
        )

        # Fire transcript callbacks (best-effort, independent). seq is
        # passed as a kwarg so existing 3-arg callbacks keep working
        # (Python silently drops unknown kwargs only with **kwargs sinks,
        # so we try the new shape first and fall back to legacy on TypeError).
        for cb in list(session.transcript_callbacks):
            try:
                try:
                    await cb(session_id, text, source, seq=seq)
                except TypeError:
                    await cb(session_id, text, source)
            except Exception as e:
                session.metrics.callback_failures += 1
                logger.debug(f"[Coach] transcript_cb failed: {e}")

        response: dict[str, Any] = {"type": "ack", "transcript": text}

        # Silence warm-up: don't evaluate while we're still re-arming
        if session.silence_pending:
            session.metrics.segments_dropped_silence_pending += 1
            return response

        # Trigger gate when EITHER the segment count or the elapsed-speech
        # window is reached. Fast talkers hit the count, slow meetings hit
        # the time-based window.
        count_ready = session.new_since_eval >= profile["segments"]
        time_ready = seconds_since_eval >= profile["seconds"] and session.new_since_eval > 0
        if count_ready or time_ready:
            tip = await self._maybe_evaluate(session, profile)
            if tip:
                response["type"] = "tip"
                response["tip"] = tip

        return response

    # Backward-compat shim for the original name
    async def add_segment(
        self,
        session_id: str,
        text: str,
        source: str = "mic",
        screenshot_b64: str | None = None,
    ) -> dict:
        return await self.add_transcript(session_id, text, source, screenshot_b64)

    async def ask_now(
        self,
        session_id: str,
        screenshot_b64: str | None = None,
    ) -> dict:
        """Hotkey-triggered "force a tip now" path.

        Skips gate1 entirely — the hotkey IS the user's explicit
        consent that THIS is the moment they want a tip. Running
        gate1 on top would only add latency and risk the model
        deciding "not now" against the user's expressed wish.

        Pulls the latest 15 segments + the freshly-captured screen
        (caller passes one in if it could capture; otherwise the
        session's cached frame is used) and runs gate2 directly.
        Tips emitted this way are routed through the same
        tip_callbacks as auto-triggered ones, so the renderer's
        notch overlay handles them identically.

        Cooldown: 3 seconds between hotkey fires per session. A user
        holding both Cmd keys down can otherwise generate dozens of
        gate2 calls per minute; the cooldown caps cost.

        Returns:
          {"tip": <text>}  on success
          {"tip": ""}      when the model declined (legal empty tip)
          {"error": <code>} for throttled / no-session / upstream errors
        """
        session = self._sessions.get(session_id)
        if not session:
            return {"error": "no_active_session"}

        now = time.time()
        if (now - session.last_ask_now_at) < 3.0:
            elapsed = now - session.last_ask_now_at
            logger.info(
                f"[Coach] ask_now throttled session={session_id} "
                f"elapsed={elapsed:.2f}s/3.0s"
            )
            return {"error": "throttled", "retry_after_ms": int((3.0 - elapsed) * 1000)}
        session.last_ask_now_at = now

        # Override the cached screenshot with the fresh hotkey-time
        # one if supplied. Otherwise we fall back to whatever the
        # auto-coaching pipeline last cached.
        if screenshot_b64:
            session.latest_screenshot_b64 = screenshot_b64

        # Need *something* to talk about. A user pressing the hotkey
        # before they've said anything would otherwise get a tip
        # against empty context.
        if len(session.segments) == 0:
            return {"error": "no_transcript_yet"}

        conversation = self._assemble_transcript(session, last_n=15)
        kg_context = self._kg_context()
        recent_tip_texts = [t.text for t in session.tips[-5:]]

        shot = session.latest_screenshot_b64
        logger.info(
            f"[Coach] ask_now.eval session={session_id} "
            f"screenshot={'present(' + str(len(shot)) + 'b)' if shot else 'none'} "
            f"segments={len(session.segments)} recent_tips={len(recent_tip_texts)}"
        )

        gate2_t = time.time()
        try:
            tip_text = await gate_pipeline.generate_tip(
                self.llm,
                self.gate_model,
                conversation,
                session.user_context,
                kg_context,
                language=session.language,
                recent_tips=recent_tip_texts,
                screenshot_b64=session.latest_screenshot_b64,
            )
        except Exception as e:
            logger.warning(f"[Coach] ask_now gate2 failed: {e}")
            return {"error": "upstream_error", "detail": str(e)}

        gate2_latency_ms = (time.time() - gate2_t) * 1000

        if not tip_text:
            logger.info(
                f"[Coach] ask_now empty-tip session={session_id} "
                f"latency_ms={gate2_latency_ms:.0f}"
            )
            return {"tip": ""}

        # Enforce length cap (prompts alone aren't reliable).
        tip_text, was_truncated = _truncate_tip(tip_text, MAX_TIP_LEN)

        # Update session bookkeeping the same way _maybe_evaluate does
        # so the rate-limit / recent-tips dedup keep working.
        session.last_tip_at = now
        session.tips_sent += 1
        session.metrics.tips_emitted += 1

        tip_seq = session.next_seq
        session.next_seq += 1
        confidence = 1.0  # explicit user request — no gate1 score to inherit
        session.tips.append(TipRecord(
            text=tip_text,
            confidence=confidence,
            timestamp=now,
            seq=tip_seq,
        ))
        if len(session.tips) > MAX_TIPS_PER_SESSION:
            session.tips.pop(0)

        logger.info(
            f"[Coach] ask_now tip.emit session={session_id} "
            f"latency_ms={gate2_latency_ms:.0f} len={len(tip_text)} "
            f"truncated={was_truncated} seq={tip_seq} tip={tip_text!r}"
        )

        # Fire tip callbacks so the desktop notch displays this tip
        # exactly like an auto-coaching tip.
        for cb in list(session.tip_callbacks):
            try:
                try:
                    await cb(session.session_id, tip_text, confidence, seq=tip_seq, timestamp=now)
                except TypeError:
                    await cb(session.session_id, tip_text, confidence)
            except Exception as e:
                session.metrics.callback_failures += 1
                logger.warning(f"[Coach] ask_now tip_cb failed: {e}")

        return {
            "tip": tip_text,
            "truncated": was_truncated,
            "seq": tip_seq,
            "timestamp": now,
            "latency_ms": gate2_latency_ms,
        }

    # ── Internal: gate pipeline ───────────────────────────────────────────────

    async def _maybe_evaluate(
        self,
        session: CoachingSession,
        profile: dict[str, int] | None = None,
    ) -> dict | None:
        """Run the gate pipeline. Returns tip dict if emitted, else None."""
        if session.eval_lock.locked():
            return None
        if profile is None:
            profile = FREQUENCY_PROFILES.get(session.frequency, FREQUENCY_PROFILES["moderate"])
        async with session.eval_lock:
            session.new_since_eval = 0
            session.last_eval_at = time.time()
            now = time.time()

            if session.tips_sent >= MAX_TIPS_PER_SESSION:
                return None
            rate_limit = profile.get("rate_limit_s", 60)
            if now - session.last_tip_at < rate_limit:
                session.metrics.tips_blocked_rate_limit += 1
                seconds_since = now - session.last_tip_at
                logger.debug(
                    f"[Coach] rate-limited ({seconds_since:.0f}s/{rate_limit}s)"
                )
                await self._emit_gate_decision(
                    session, "rate_limit", passed=False,
                    reason=f"{seconds_since:.0f}s since last tip, need {rate_limit}s",
                    extras={"seconds_since": round(seconds_since, 1),
                            "rate_limit_s": rate_limit},
                )
                return None

            conversation = self._assemble_transcript(session, last_n=20)
            # Very short conversations aren't worth a gate call
            word_count = sum(len(s.text.split()) for s in session.segments[-10:])
            if word_count < MIN_WORDS_FOR_EVAL:
                return None

            kg_context = self._kg_context()
            threshold = gate_pipeline.FREQUENCY_THRESHOLDS.get(
                session.frequency, 0.60
            )

            t0 = time.time()
            session.metrics.gate_evaluations += 1

            # Recent tips passed to both gates so the LLM can avoid
            # repeating itself across turns. 5 covers the typical
            # rate-limit window comfortably without bloating the prompt.
            recent_tip_texts = [t.text for t in session.tips[-5:]]

            # Stage 1: relevance (session override > manager default)
            mode = session.gate_mode if session.gate_mode in gate_pipeline.GATE_MODES else self.gate_mode
            gate1_t = time.time()

            # Faz E: latest cached screenshot fed to gate1 + gate2. We
            # deliberately DO NOT clear the cache after use — the
            # desktop's pHash dedup means it only sends a new frame
            # when the screen visibly changes; if we cleared the bot
            # cache after each gate eval the model would lose visual
            # context until the next screen change, which often
            # contradicts the user's flow ("I'm still looking at the
            # same email, why doesn't Coach see it anymore").
            # Cache lifetime = session lifetime; a new screen capture
            # from the desktop overwrites, an unchanged screen leaves
            # the cache intact.
            screenshot_for_eval = session.latest_screenshot_b64
            uc_preview = (session.user_context[:80] + '…') if len(session.user_context) > 80 else session.user_context
            uc_display = repr(uc_preview) if uc_preview else "EMPTY"
            shot_display = (
                f"present({len(screenshot_for_eval)}b)"
                if screenshot_for_eval else "none"
            )
            kg_len = len(kg_context)
            logger.info(
                f"[Coach] gate.eval session={session.session_id} "
                f"screenshot={shot_display} "
                f"mode={mode} freq={session.frequency} "
                f"user_ctx={uc_display} "
                f"kg_ctx_chars={kg_len}"
            )

            ok, score, reason = await gate_pipeline.relevance_gate(
                self.llm, self.gate_model,
                conversation, session.user_context, kg_context, threshold,
                mode=mode,
                recent_tips=recent_tip_texts,
                screenshot_b64=screenshot_for_eval,
            )

            gate1_latency_ms = (time.time() - gate1_t) * 1000
            await self._emit_gate_decision(
                session, "gate1", passed=ok,
                score=score, reason=reason, threshold=threshold,
                latency_ms=gate1_latency_ms,
                extras={
                    "mode": mode,
                    "recent_tips_count": len(recent_tip_texts),
                    "has_screenshot": screenshot_for_eval is not None,
                },
            )
            if not ok:
                session.metrics.gate_latency_total_s += time.time() - t0
                return None
            session.metrics.gate1_passes += 1

            # Stage 2: generate
            gate2_t = time.time()
            tip_text = await gate_pipeline.generate_tip(
                self.llm, self.gate_model,
                conversation, session.user_context, kg_context,
                language=session.language,
                recent_tips=recent_tip_texts,
                screenshot_b64=screenshot_for_eval,
            )
            gate2_latency_ms = (time.time() - gate2_t) * 1000
            await self._emit_gate_decision(
                session, "gate2", passed=bool(tip_text),
                reason=("tip generated" if tip_text else "empty/invalid tip text"),
                latency_ms=gate2_latency_ms,
                extras={"tip_len": len(tip_text or "")},
            )
            if not tip_text:
                session.metrics.gate_latency_total_s += time.time() - t0
                return None
            session.metrics.gate2_passes += 1

            # Stage 3: critic (optional — can be overly strict)
            if self.use_critic:
                critic_t = time.time()
                is_useful = await gate_pipeline.critic(
                    self.llm, self.gate_model,
                    tip_text, conversation, session.user_context,
                )
                critic_latency_ms = (time.time() - critic_t) * 1000
                await self._emit_gate_decision(
                    session, "critic", passed=is_useful,
                    reason=("useful" if is_useful else "rejected as not useful"),
                    latency_ms=critic_latency_ms,
                )
                if not is_useful:
                    session.metrics.critic_rejects += 1
                    session.metrics.gate_latency_total_s += time.time() - t0
                    logger.debug(f"[Coach] critic rejected tip: {tip_text!r}")
                    return None

            # Enforce length cap at the code level — prompts alone aren't
            # reliable. Truncate on a sentence / word boundary.
            tip_text, was_truncated = _truncate_tip(tip_text, MAX_TIP_LEN)
            if was_truncated:
                session.metrics.tips_truncated += 1

            session.last_tip_at = now
            session.tips_sent += 1
            session.metrics.tips_emitted += 1
            session.metrics.gate_latency_total_s += time.time() - t0

            tip_seq = session.next_seq
            session.next_seq += 1
            confidence = round(score, 3)
            session.tips.append(TipRecord(
                text=tip_text,
                confidence=confidence,
                timestamp=now,
                seq=tip_seq,
            ))
            if len(session.tips) > MAX_TIPS_PER_SESSION:
                session.tips.pop(0)

            tip = {
                "text": tip_text,
                "confidence": confidence,
                "timestamp": now,
                "seq": tip_seq,
            }
            logger.info(
                f"[Coach] tip.emit session={session.session_id} "
                f"score={score:.2f} freq={session.frequency} "
                f"mode={mode} len={len(tip_text)} "
                f"truncated={was_truncated} seq={tip_seq} tip={tip_text!r}"
            )

            # Fire tip callbacks (best-effort, independent). seq passed as
            # kwarg so legacy 3-arg callbacks keep working.
            for cb in list(session.tip_callbacks):
                try:
                    try:
                        await cb(session.session_id, tip_text, confidence, seq=tip_seq, timestamp=now)
                    except TypeError:
                        await cb(session.session_id, tip_text, confidence)
                except Exception as e:
                    session.metrics.callback_failures += 1
                    logger.warning(f"[Coach] tip_cb failed: {e}")

            return tip

    # ── Internal: finalization ────────────────────────────────────────────────

    async def _finalize(self, session: CoachingSession) -> dict:
        """Run post-meeting processing. Every step is isolated."""
        result: dict[str, Any] = {
            "summary": "",
            "entities_added": 0,
            "memory_updated": False,
            "artifact_id": None,
            "errors": [],
        }
        transcript = self._assemble_transcript(session)

        # 1. Summary (best-effort)
        try:
            result["summary"] = await gate_pipeline.summarize_meeting(
                self.llm, self.summary_model, transcript, session.user_context
            ) or ""
        except Exception as e:
            msg = f"summarize_failed: {e}"
            logger.warning(f"[Coach] {msg}")
            result["errors"].append(msg)

        # 2. Entities → KG
        if result["summary"] and self.knowledge_graph is not None:
            try:
                entities = await gate_pipeline.extract_entities(
                    self.llm, self.summary_model, result["summary"]
                )
                added = 0
                for e in entities:
                    try:
                        self.knowledge_graph.add_triple(
                            subject=str(e.get("subject", "")).strip(),
                            predicate=str(e.get("predicate", "")).strip(),
                            obj=str(e.get("object", "")).strip(),
                            subject_type=str(e.get("subject_type", "")).strip(),
                            object_type=str(e.get("object_type", "")).strip(),
                            source=f"coaching:{session.session_id}",
                        )
                        added += 1
                    except Exception as ex:
                        logger.debug(f"[Coach] skipped triple {e}: {ex}")
                result["entities_added"] = added
            except Exception as e:
                msg = f"entity_extraction_failed: {e}"
                logger.warning(f"[Coach] {msg}")
                result["errors"].append(msg)

        # 3. Memory
        if result["summary"] and self.memory_path is not None:
            try:
                self._append_memory(session, result["summary"])
                result["memory_updated"] = True
            except Exception as e:
                msg = f"memory_append_failed: {e}"
                logger.warning(f"[Coach] {msg}")
                result["errors"].append(msg)

        # 4. Artifact
        if self.artifact_store is not None:
            try:
                artifact = self._save_artifact(session, transcript, result["summary"])
                if artifact:
                    result["artifact_id"] = artifact.get("id")
            except Exception as e:
                msg = f"artifact_save_failed: {e}"
                logger.warning(f"[Coach] {msg}")
                result["errors"].append(msg)

        logger.info(
            f"[Coach] session '{session.session_id}' finalized — "
            f"entities={result['entities_added']}, "
            f"summary_len={len(result['summary'])}, "
            f"errors={len(result['errors'])}"
        )

        # Notify registered finalization callbacks
        for cb in list(session.finalized_callbacks):
            try:
                await cb(session.session_id, result)
            except Exception as e:
                logger.debug(f"[Coach] finalized_cb failed: {e}")

        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _kg_context(self) -> str:
        """Background facts about the user fed to the gate.

        Combines two sources under a single label the prompt already
        knows ("USER KNOWLEDGE BASE"):
          1. KnowledgeGraph entity-relationship summary (structured).
          2. Tail of MEMORY.md — the last few meeting summaries in
             markdown. KG is great at "who is connected to whom" but
             loses prose detail (action items, decisions, sentiment).
             The memory tail brings prose back.

        Either source may be empty; we omit empty sections to keep the
        gate's prompt token cost minimal when nothing's there.
        """
        parts: list[str] = []

        if self.knowledge_graph:
            try:
                summary = self.knowledge_graph.summary()
                if summary:
                    if len(summary) > KG_SUMMARY_MAX_CHARS:
                        summary = summary[:KG_SUMMARY_MAX_CHARS] + "\n..."
                    parts.append("Entities & relationships:\n" + summary)
            except Exception as e:
                logger.debug(f"[Coach] kg.summary failed: {e}")

        memory_tail = self._memory_context()
        if memory_tail:
            parts.append("Recent meeting memories:\n" + memory_tail)

        return "\n\n".join(parts)

    def _memory_context(self) -> str:
        """Tail of MEMORY.md — last few finalized meeting summaries.

        The bot writes a ``## Meeting — YYYY-MM-DD HH:MM`` block to
        MEMORY.md after each session finalises. Reading the last
        ``MEMORY_TAIL_MAX_CHARS`` covers ~3-5 recent meetings, giving
        the gate prose-level recall ("you said Mehmet was ignoring
        you last Tuesday") that the KG triple summary doesn't capture.

        Best-effort: missing file, permission errors, or other IO
        problems silently return "" so the gate degrades gracefully
        rather than failing the eval.
        """
        if not self.memory_path:
            return ""
        try:
            path = Path(self.memory_path)
            if not path.exists():
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text:
                return ""
            if len(text) > MEMORY_TAIL_MAX_CHARS:
                # Try to start at a meeting boundary so we don't
                # truncate mid-summary. Fall back to a hard slice if
                # no header is found in the tail window.
                tail = text[-MEMORY_TAIL_MAX_CHARS:]
                header_idx = tail.find("## ")
                if header_idx > 0 and header_idx < MEMORY_TAIL_MAX_CHARS // 2:
                    tail = tail[header_idx:]
                return tail
            return text
        except Exception as e:
            logger.debug(f"[Coach] memory tail read failed: {e}")
            return ""

    def _assemble_transcript(
        self,
        session: CoachingSession,
        last_n: int | None = None,
    ) -> str:
        """Render the rolling transcript with speaker prefixes.

        Source-as-speaker mapping:
          - ``mic`` → "[YOU]:"   — the user's own microphone, almost
            always them speaking (single-mic videoconference setup).
          - ``system`` → "[OTHER]:" — system audio output, i.e. the
            remote participant on Zoom / Teams / Meet / FaceTime.

        Using "[YOU]" / "[OTHER]" instead of the older "[system]" tag
        gives the gate prompt unambiguous semantics: tips should help
        [YOU], not coach [OTHER]. Older labels conflated "system" the
        speaker with "system" the prompt role, and the model
        occasionally treated [OTHER]'s commitments as the user's.

        In-person meetings (multiple speakers on one mic) still collapse
        onto [YOU] here — proper many-speaker diarization needs Scribe
        Realtime's speaker_id field and isn't covered by this helper.
        """
        segs = session.segments[-last_n:] if last_n else session.segments
        lines = []
        for s in segs:
            prefix = "[OTHER]: " if s.source == "system" else "[YOU]: "
            lines.append(f"{prefix}{s.text}")
        return "\n".join(lines)

    def _append_memory(self, session: CoachingSession, summary: str) -> None:
        if not self.memory_path:
            return
        path = Path(self.memory_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        date = datetime.fromtimestamp(session.started_at).strftime("%Y-%m-%d %H:%M")
        block = f"\n\n## Meeting — {date}\n\n{summary}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(block)

    def _save_artifact(
        self,
        session: CoachingSession,
        transcript: str,
        summary: str,
    ) -> dict | None:
        if not self.artifact_store:
            return None
        date = datetime.fromtimestamp(session.started_at).strftime("%Y-%m-%d %H:%M")
        title = f"Meeting — {date}"
        body_parts: list[str] = []
        if summary:
            body_parts.append(f"## Summary\n\n{summary}\n")
        body_parts.append(f"## Full Transcript\n\n```\n{transcript}\n```")
        content = "\n".join(body_parts)
        metadata = {
            "source": "coaching",
            "session_id": session.session_id,
            "started_at": session.started_at,
            "duration": time.time() - session.started_at,
            "segments": len(session.segments),
            "tips_sent": session.tips_sent,
        }
        return self.artifact_store.create(
            type="markdown",
            title=title,
            content=content,
            metadata=metadata,
            tags=["meeting", "coaching"],
        )

    # ── Watchdog: enforce max session duration ────────────────────────────────

    def _ensure_watchdog(self) -> None:
        """Start the watchdog task if not already running."""
        if self._watchdog_task and not self._watchdog_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watchdog_task = loop.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        """Auto-stop sessions that exceed max_session_seconds."""
        try:
            while self._sessions:
                await asyncio.sleep(60)
                now = time.time()
                overdue = [
                    sid for sid, s in list(self._sessions.items())
                    if now - s.started_at > self.max_session_seconds
                ]
                for sid in overdue:
                    logger.warning(
                        f"[Coach] auto-stopping session '{sid}' "
                        f"(exceeded {self.max_session_seconds}s)"
                    )
                    try:
                        await self.stop(sid, background_finalize=True)
                    except Exception as e:
                        logger.warning(f"[Coach] watchdog stop failed: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Coach] watchdog crashed: {e}")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def shutdown_all(self) -> None:
        """Best-effort cleanup of all active sessions and pending tasks."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            self._sessions.pop(sid, None)
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        pending = list(self._pending_tasks)
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass
        if session_ids:
            logger.info(f"[Coach] shutdown: {len(session_ids)} sessions cleared")
