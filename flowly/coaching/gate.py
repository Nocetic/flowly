"""3-stage gate pipeline for coaching tips.

Inspired by OMI's relevance → generate → critic pattern. Most conversations
get rejected at stage 1 with a cheap LLM call. Only exceptional moments
reach stage 2 (generate tip) and stage 3 (final usefulness check).
"""

import json
import re
from typing import Any

from loguru import logger

from ..providers.base import LLMProvider

FREQUENCY_THRESHOLDS: dict[str, float] = {
    "selective": 0.80,   # only when genuinely useful
    "moderate": 0.60,    # balanced — default
    "proactive": 0.40,   # frequent, helpful interjections
}


# Mode controls gate prompt emphasis.
# - "assistant" (default): interject whenever Flowly can genuinely help —
#   answer questions, clarify unknowns, surface KG info, catch mistakes.
#   Fits natural conversation where the user wants an AI helper.
# - "guardian": interject ONLY at critical moments (wrong numbers, broken
#   commitments, missed time-sensitive actions). For focused negotiations.
GATE_MODES = ("assistant", "guardian")


_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n?|\n?\s*```\s*$")


def _strip_markdown_fences(text: str) -> str:
    """Remove ``` / ```json code fences that some models wrap JSON in.

    Why: claude-haiku-4.5 (and other Anthropic models) regularly return
    ```json\\n{...}\\n``` even when the prompt says 'JSON only, no fences'.
    Stripping is cheap and preserves a clean text-form for the parsers
    below. Idempotent — text without fences passes through unchanged.
    """
    t = text.strip()
    # Repeatedly strip leading/trailing fence markers — handles nested or
    # double-fenced output without exotic regex.
    for _ in range(2):
        before = t
        t = _FENCE_RE.sub("", t).strip()
        if t == before:
            break
    return t


def _try_load_json(text: str) -> Any:
    """Best-effort JSON load: strips fences, then tries to parse.

    Returns the parsed object, or None on failure. Never raises.
    """
    cleaned = _strip_markdown_fences(text)
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _parse_score(text: str) -> float:
    """Extract a score 0-1 from LLM output. Tolerant of JSON/plaintext/fenced.

    Rules:
      - JSON object with "score" key (fenced or unfenced) → use, clamp to [0, 1]
      - Raw decimal in (0, 1] range → use as-is
      - Integer in (1, 100] range → treat as percentage (divide by 100)
      - Value outside [0, 100] → clamp to [0, 1]
    """
    if not text:
        return 0.0
    # JSON path (handles markdown-fenced output too)
    obj = _try_load_json(text)
    if isinstance(obj, dict) and "score" in obj:
        try:
            val = float(obj["score"])
            return max(0.0, min(1.0, val))
        except (TypeError, ValueError):
            pass
    # Fallback: regex for first numeric token
    m = re.search(r"(?:score[^\d]*)?(\d*\.?\d+)", text)
    if m:
        try:
            val = float(m.group(1))
            # Heuristic: if it looks like a percentage (integer > 1 with no
            # decimal point in the original token), convert. Otherwise clamp.
            if val > 1.0 and "." not in m.group(1) and val <= 100.0:
                val = val / 100.0
            return max(0.0, min(1.0, val))
        except ValueError:
            pass
    return 0.0


def _parse_bool(text: str) -> bool:
    """Parse a yes/no or true/false from LLM output (fenced JSON tolerated)."""
    if not text:
        return False
    obj = _try_load_json(text)
    if isinstance(obj, dict):
        for k in ("useful", "is_useful", "pass", "result"):
            if k in obj:
                return bool(obj[k])
    if isinstance(obj, bool):
        return obj
    t = _strip_markdown_fences(text).lower()
    return t.startswith(("yes", "true", "1", "pass", "useful"))


_ASSISTANT_SYSTEM = (
    "Score whether THIS moment deserves a coaching tip (0.0–1.0).\n"
    "JSON only. No commentary. No engagement with topic content.\n"
    "\n"
    "# Speakers\n"
    "Lines prefixed [YOU]: are the user (tip audience).\n"
    "Lines prefixed [OTHER]: are remote participants on a call.\n"
    "Tips coach [YOU]. [OTHER]'s speech matters only when it gives [YOU]\n"
    "something to act on (push back, fact-check, answer).\n"
    "\n"
    "# Rule of priority\n"
    "USER CONTEXT overrides everything below. If the user told you what\n"
    "to watch for and the moment matches, score ≥0.70 — even on casual-\n"
    "looking surface. Default criteria apply only when no part of USER\n"
    "CONTEXT is triggered.\n"
    "\n"
    "# Reject (≤0.30) when ANY of:\n"
    "- The user has ALREADY noticed / mentioned / acted on the very\n"
    "  thing a tip would point out. The user is ahead — don't repeat.\n"
    "- A tip about this would paraphrase ANY entry in RECENT TIPS.\n"
    "- Mid-sentence, filler, small talk, ambient chatter.\n"
    "- Pure greeting / closing / acknowledgement.\n"
    "\n"
    "# Score high (≥0.70) when:\n"
    "- USER CONTEXT directly applies AND the moment hasn't been covered\n"
    "  yet by a RECENT TIP.\n"
    "- The user is about to make a decision or commitment with concrete\n"
    "  numbers / terms / names AND no tip has flagged the same hazard.\n"
    "- Visible screen content directly contradicts what's being said.\n"
    "- A factual claim is testable AND user asked for fact-checking.\n"
    "\n"
    "# Score medium (0.40–0.60) when:\n"
    "- Substantive content where a fact or distinction would help,\n"
    "  AND no recent tip has covered it.\n"
    "\n"
    "# Output\n"
    "{\"score\": <0.0–1.0>, \"reason\": \"<6–14 words>\"}\n"
    "Reason is REQUIRED. Be decisive — 0.5 means you couldn't decide,\n"
    "which is wrong. Pick a side."
)

_GUARDIAN_SYSTEM = (
    "You are a strict guardian gate for a meeting assistant. The assistant "
    "must only interrupt at CRITICAL moments. Reject 90%+ of the time.\n\n"
    "GOLDEN RULE: USER CONTEXT OVERRIDES THE 90% REJECT DEFAULT.\n"
    "If USER CONTEXT names a specific behaviour to guard against (e.g. "
    "'stop me from messaging X', 'warn me if I commit to bad terms') AND "
    "the current moment matches that behaviour, score HIGH (≥0.70) even "
    "if the surface looks like casual chat. Don't reject because it 'lacks "
    "factual hooks' — the user told you what to watch for; fire on THAT.\n\n"
    "PASS (score >= 0.70) ONLY when ONE of these applies:\n"
    " - The user's stated guardian intent (from USER CONTEXT) is triggered "
    "   by the current moment.\n"
    " - User is about to make a concrete mistake (wrong number, bad "
    "   commitment, agreeing to unfavourable terms).\n"
    " - Someone said something directly conflicting with the user's plans, "
    "   history, or knowledge-base facts.\n"
    " - A time-sensitive action is about to be missed.\n"
    " - Wrong information is being shared and the correct version is in "
    "   the user's knowledge base.\n\n"
    "Questions, unknowns, small talk, and general curiosity → LOW SCORE "
    "(unless USER CONTEXT promotes them to relevance).\n"
    "Respond with ONLY a JSON object: {\"score\": 0.0-1.0, \"reason\": \"<5-12 words>\"}\n"
    "Reason is REQUIRED."
)

# ─── Screen-aware blocks (appended only when a screenshot is attached) ─────
#
# Kept as separate constants so the prompt cache stays warm for the
# common text-only path — appending a 600-token block only when an
# image is actually present avoids invalidating the cached system
# prefix on every single gate call.
#
# Score-side block: tells the gate how to weigh the screen image
# against the audio transcript. The screen alone NEVER justifies a
# tip — there must also be a high-signal audio moment.
#
# Generate-side block: tells the tip writer to reference specific
# visible details (not "screenshot" / "image"), and most importantly
# tells it NOT to fabricate content when the image is too blurry to
# read — that's the Anthropic-vision failure mode we saw in the
# field where the model invented "Quick follow-up with 3 repetitions
# of I know you" against an actual email that said no such thing.

_SCREEN_BLOCK_SCORE = (
    "\n\n"
    "════════════════════════════════════════════════════════════════════\n"
    "SCREEN CONTEXT (attached only when a screen capture is present)\n"
    "════════════════════════════════════════════════════════════════════\n"
    "The user's screen at this moment is provided alongside the conversation.\n"
    "\n"
    "Use it as ADDITIONAL signal — never as the primary justification:\n"
    " - Score HIGH (+0.10–0.20 boost) when the screen DIRECTLY corroborates\n"
    "   or conflicts with what was just said (a quoted number contradicted\n"
    "   by the visible email, etc.).\n"
    " - Score HIGH when the user's audio references something visible\n"
    "   ('look at this commit', 'what does it say in line 42') — visual\n"
    "   anchoring makes the tip far more useful.\n"
    " - Score MEDIUM if the screen is topically related but not directly\n"
    "   referenced in audio (slides on display while discussing them).\n"
    " - Score LOW for purely visual states (user reading silently, code\n"
    "   compiling, screensaver). A rich screen with quiet audio is NOT\n"
    "   reason to interrupt.\n"
    "\n"
    "NEVER score high because 'the screen shows something interesting'\n"
    "alone."
)

_SCREEN_BLOCK_GENERATE = (
    "\n\n"
    "════════════════════════════════════════════════════════════════════\n"
    "SCREEN CONTEXT (attached only when a screen capture is present)\n"
    "════════════════════════════════════════════════════════════════════\n"
    "The user's screen at this moment is provided alongside the conversation.\n"
    "\n"
    "Rules for using it:\n"
    " - If something on the screen DIRECTLY relates to the moment (an email\n"
    "   being quoted, a number being discussed, a slide being referenced),\n"
    "   incorporate the SPECIFIC visible detail in your tip.\n"
    " - If the screen is irrelevant to what's being said, IGNORE it\n"
    "   completely. Don't force a connection that isn't there.\n"
    " - NEVER use the words 'screenshot', 'image', 'screen capture',\n"
    "   'looking at', or 'I see'. Refer to specific things by their name\n"
    "   ('the email from John', 'the chart on the slide', 'line 42 in the\n"
    "   function').\n"
    " - NEVER summarize what's on screen — the user is already looking at it.\n"
    " - If the visible text conflicts with what was just said audibly, that\n"
    "   conflict IS the tip. State it directly.\n"
    " - Do NOT fabricate specific text, names, numbers, or quotes you\n"
    "   cannot read directly in the image. If you reference something\n"
    "   visible, it must actually be visible. When the image gives you\n"
    "   no usable visual evidence, just rely on the audio context and\n"
    "   write a tip from that — don't comment on the image at all.\n"
)


def _build_user_message_content(text: str, screenshot_b64: str | None) -> Any:
    """Construct the user-message ``content`` field, picking the
    optimal shape for the LLM provider:

      - No screenshot → plain string (zero-alloc hot path, identical
        wire shape to the original legacy gate so prompt caching at
        the provider survives across screen-on / screen-off turns).
      - Screenshot present → OpenAI-style multimodal content array
        with the image part FIRST so the model has visual context
        loaded before reading the question (Anthropic best practice).

    The Flowly proxy + OpenRouter + Anthropic all natively accept
    the multimodal shape — no provider-specific branching needed
    here.
    """
    if not screenshot_b64:
        return text
    # Auto-detect the wire format from the base64 prefix and declare
    # the mime type accordingly. Anthropic (via Bedrock or direct)
    # validates the declared mime type against the actual bytes and
    # 400s the request on mismatch:
    #
    #   messages.0.content.0.image.source.base64: The image was
    #   specified using the image/png media type, but the image
    #   appears to be a image/jpeg image
    #
    # Multiple capture paths feed this helper (Electron desktopCapturer
    # → PNG, macOS native helper → JPEG, future paths → maybe WebP),
    # so we can't hardcode either. Reading 1-2 chars of the base64
    # prefix is enough to discriminate without decoding the payload:
    #   PNG  bytes start "\x89PNG"  → base64 "iVBORw0…"
    #   JPEG bytes start "\xFF\xD8" → base64 "/9j/…"
    #   WebP bytes start "RIFF"     → base64 "UklGR…"
    mime = _detect_image_mime(screenshot_b64)
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{screenshot_b64}"},
        },
        {"type": "text", "text": text},
    ]


def _detect_image_mime(b64: str) -> str:
    """Best-effort mime sniff from the first chars of a base64 payload.

    Defaults to ``image/jpeg`` on the unknown branch — it's the
    historically dominant format and most providers tolerate a JPEG
    payload mis-labelled as JPEG more gracefully than the inverse.
    """
    if not b64:
        return "image/jpeg"
    head = b64[:8]
    if head.startswith("iVBORw0"):
        return "image/png"
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("UklGR"):
        return "image/webp"
    if head.startswith("R0lGOD"):
        return "image/gif"
    return "image/jpeg"


_REASON_RE = re.compile(
    r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def _parse_reason(text: str) -> str:
    """Pull a short 'reason' field out of JSON output (fenced or not).

    Falls back to a regex if JSON parsing fails — the prompt requires the
    reason field but a model occasionally trails commentary after the JSON
    object (or wraps in ```fences``` and adds a partial ```` close), which
    breaks strict json.loads. Regex catches those cases.

    Returns empty string only when both JSON and regex miss.
    """
    if not text:
        return ""
    obj = _try_load_json(text)
    if isinstance(obj, dict):
        r = obj.get("reason")
        if isinstance(r, str) and r.strip():
            return r.strip()[:120]
    # Regex fallback for malformed/partial JSON
    m = _REASON_RE.search(text)
    if m:
        # Unescape simple JSON string escapes (\\", \\n, \\\\)
        raw = m.group(1).replace('\\"', '"').replace("\\n", " ").replace("\\\\", "\\")
        if raw.strip():
            return raw.strip()[:120]
    return ""


def _format_recent_tips(recent_tips: list[str]) -> str:
    """Render the recent-tips block for the gate user message.

    Returns '(none yet)' when empty so the LLM sees a stable structure
    every turn — easier to follow than a missing/optional section.
    """
    if not recent_tips:
        return "(none yet)"
    return "\n".join(
        f"  {i + 1}. {t}" for i, t in enumerate(recent_tips)
    )


async def relevance_gate(
    provider: LLMProvider,
    model: str,
    conversation: str,
    user_context: str,
    kg_context: str,
    threshold: float,
    mode: str = "assistant",
    recent_tips: list[str] | None = None,
    screenshot_b64: str | None = None,
) -> tuple[bool, float, str]:
    """Stage 1: decide whether to interject at all.

    Two prompt modes:
      - "assistant" (default): helper-first — answer questions, clarify,
        surface KG info, catch mistakes.
      - "guardian": strict — only critical mistakes / conflicts / deadlines.

    ``recent_tips`` is the list of recently-emitted tip texts (last few in
    chronological order). Including them prevents the gate from scoring
    "speak up" on a topic the user already saw advice on — the most common
    cause of "Coach keeps repeating itself" reports.

    ``screenshot_b64`` is an optional base64 JPEG of the user's screen
    at this moment. When present we append the screen-aware block to
    the system prompt and build a multimodal user message; the Flowly
    proxy + OpenRouter forward both parts to the vision-capable model
    (Anthropic Haiku 4.5 by default). When absent we stay on the legacy
    text-only path with zero overhead — prompt cache stays warm for
    the 80%+ of turns that have no image attached.

    Returns ``(passed, score, reason)`` — reason is the LLM's brief self-
    explanation, surfaced in structured logs for diagnostics.

    Failure handling:
      - If the LLM returns empty content (refusal / moderation / hiccup),
        we retry once with a tiny temperature bump to break determinism.
      - If retry also empty, score is treated as 0.0 (reject) but logged
        as a structured failure so we can spot model-side issues without
        spamming a noisy fallback.
    """
    base_system = _GUARDIAN_SYSTEM if mode == "guardian" else _ASSISTANT_SYSTEM
    system = base_system + _SCREEN_BLOCK_SCORE if screenshot_b64 else base_system
    # RECENT TIPS first so the model checks dedup BEFORE inspecting the
    # current moment — same fix Tony Seah used to drop repetition from
    # 15% to 0% in his production gate.
    user_text = (
        f"RECENT TIPS YOU ALREADY GAVE THIS SESSION "
        f"(score LOW if this moment would only repeat them):\n"
        f"{_format_recent_tips(recent_tips or [])}\n\n"
        f"USER CONTEXT (their stated intent for this session):\n"
        f"{user_context or '(none provided)'}\n\n"
        f"USER KNOWLEDGE BASE (recent facts):\n{kg_context or '(empty)'}\n\n"
        f"RECENT CONVERSATION:\n{conversation}\n\n"
        "Score this moment per the rules in the system prompt. "
        "Return ONLY the JSON object — no commentary."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _build_user_message_content(user_text, screenshot_b64)},
    ]

    async def _ask(temperature: float) -> tuple[str, str]:
        """Returns (content, finish_reason). Provider may turn exceptions
        into a normal response with finish_reason='error' and an error
        message in content, so we explicitly surface the finish reason
        rather than just returning content."""
        # max_tokens bumped from 200 → 300: gives the model room for
        # the JSON object plus the mandatory reason field without truncation.
        response = await provider.chat(
            messages=messages,
            model=model,
            max_tokens=300,
            temperature=temperature,
            timeout=15.0,
            purpose="coaching",
        )
        return (response.content or "").strip(), response.finish_reason

    def _is_failed_response(content: str, finish: str) -> bool:
        """Detect provider-side failures that look like real responses.

        OpenRouterProvider catches exceptions and returns
        LLMResponse(content='Error calling LLM: ...', finish_reason='error')
        so the agent loop can see the failure. For gate scoring this is a
        retry signal, not a 0.0 score — otherwise transient connection
        errors silently reject every segment.
        """
        if finish == "error":
            return True
        if not content:
            return True
        # Extra safety belt: even if finish_reason mis-fires, the content
        # itself starts with our well-known error prefix.
        if content.startswith("Error calling LLM:"):
            return True
        return False

    try:
        # 0.2 — low enough that scoring stays consistent across runs, high
        # enough to break "always emit the same score for borderline" loops
        # observed at 0.0. Anthropic / OpenAI realtime guides land scoring
        # tasks in the 0.2-0.4 band; we sit at the cautious end.
        raw, finish = await _ask(0.2)
        logger.debug(
            f"[Coach] gate1 raw mode={mode} finish={finish} "
            f"content={raw[:300]!r}"
        )
        # Provider failure (connection error, rate limit, refusal returned
        # as error-shaped response) → retry once with a temperature
        # bump so we don't hit the exact same cached failure.
        if _is_failed_response(raw, finish):
            logger.info(
                f"[Coach] gate1 retry mode={mode} reason={finish} "
                f"first_attempt={raw[:120]!r}"
            )
            raw, finish = await _ask(0.4)
            logger.debug(
                f"[Coach] gate1 retry-raw finish={finish} "
                f"content={raw[:300]!r}"
            )
        # If retry also failed, surface it loudly so we can find these in
        # logs / metrics — don't pretend a 0.0 score was a real decision.
        if _is_failed_response(raw, finish):
            logger.warning(
                f"[Coach] gate1 failed-after-retry mode={mode} "
                f"finish={finish} — treating as reject. "
                f"This may indicate API instability."
            )
            return False, 0.0, "provider_error"
        score = _parse_score(raw)
        reason = _parse_reason(raw)
        passed = score >= threshold
        logger.info(
            f"[Coach] gate1 {'pass' if passed else 'reject'} "
            f"mode={mode} score={score:.2f} thr={threshold:.2f} reason={reason!r}"
        )
        return passed, score, reason
    except Exception as e:
        logger.warning(f"[Coach] gate1 failed: {e}")
        return False, 0.0, f"error: {type(e).__name__}"


async def generate_tip(
    provider: LLMProvider,
    model: str,
    conversation: str,
    user_context: str,
    kg_context: str,
    language: str = "auto",
    recent_tips: list[str] | None = None,
    screenshot_b64: str | None = None,
) -> str | None:
    """Stage 2: generate a short, actionable tip (≤120 chars).

    ``recent_tips`` lets the model see what it has already told the user
    so it doesn't paraphrase the same advice in a continuing conversation.
    Returning the empty string is the model's signal that no NEW tip is
    worth giving — we treat that as a non-emit.

    ``screenshot_b64`` is an optional base64 JPEG of the user's screen.
    When present we append the screen-aware block to the system prompt
    (with the anti-hallucination rule) and build a multimodal user
    message. When absent we stay on the legacy text-only path.
    """
    lang_hint = (
        "Write the tip in the same language as the conversation."
        if language in ("auto", "", None)
        else f"Write the tip in language code: {language}."
    )
    base_system = (
        "You write ONE short coaching tip for [YOU] (the user). Just the\n"
        "tip text — no preamble, no quotes, no JSON.\n"
        "\n"
        "# Speakers\n"
        "[YOU]: the user — your only audience.\n"
        "[OTHER]: remote participants. Never address them. When they say\n"
        "something the user should act on, write the advice TO [YOU]\n"
        "(\"They quoted $50/seat — push back, market is $30\"), not a reply.\n"
        "\n"
        "# What to write\n"
        f"- {lang_hint}\n"
        "- Under 120 characters; aim under 80.\n"
        "- Direct, single-clause, imperative voice. No throat-clearing,\n"
        "  no 'You might want to...', no advisory cliché openings.\n"
        "- USER CONTEXT is your primary directive — when it applies,\n"
        "  serve it even if the surface looks casual.\n"
        "\n"
        "# Don't repeat yourself — STRICT\n"
        "RECENT TIPS shows what you've already said this session.\n"
        "If your candidate tip would:\n"
        "  - Say the same thing in different words,\n"
        "  - Re-recommend the same action (\"check X\" / \"verify X\" / \"look at X\"),\n"
        "  - Re-cite the same screen element you already cited,\n"
        "then DO NOT emit it. Output <no_tip/> instead.\n"
        "\n"
        "# Don't lecture the user about what they already noticed — STRICT\n"
        "If [YOU] just said \"I'll check\", \"let me look\", \"I'm reviewing\",\n"
        "\"I see\", \"noticing\", \"problem here\", or otherwise demonstrated\n"
        "awareness of the very thing your tip would point out, output\n"
        "<no_tip/>. The user is ahead of you — silence is the value-add.\n"
        "\n"
        "# Output contract\n"
        "Either the tip text itself, OR exactly: <no_tip/>\n"
        "Never write 'No tips', 'Nothing to add', or any meta-commentary."
    )
    system = base_system + _SCREEN_BLOCK_GENERATE if screenshot_b64 else base_system
    # RECENT TIPS first — at the top of the user message — so the model
    # weighs the "don't repeat" rule against the freshest possible
    # context. Putting it at the bottom (where it used to live) made
    # the model paraphrase the most recent tip ~half the time.
    user_text = (
        f"RECENT TIPS YOU ALREADY GAVE THIS SESSION "
        f"(do NOT paraphrase, do NOT re-recommend the same action):\n"
        f"{_format_recent_tips(recent_tips or [])}\n\n"
        f"USER CONTEXT:\n{user_context or '(none)'}\n\n"
        f"USER KNOWLEDGE BASE:\n{kg_context or '(empty)'}\n\n"
        f"RECENT CONVERSATION:\n{conversation}\n\n"
        "Write the tip — or <no_tip/> if everything useful was already\n"
        "said or [YOU] has already noticed it."
    )
    try:
        response = await provider.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _build_user_message_content(user_text, screenshot_b64)},
            ],
            model=model,
            max_tokens=200,
            # 0.7 — production sweet spot for short conversational
            # outputs. At 0.3 the model fell into a small set of
            # patterns and emitted near-duplicates across calls; 0.7
            # restores diversity without losing the structural rules
            # the prompt sets. (Tony Seah, Wasowski 48× study,
            # PromptLayer / Tetrate guides.)
            temperature=0.7,
            timeout=15.0,
            purpose="coaching",
        )
        tip = (response.content or "").strip().strip('"').strip("'")
        # Strip leading "Tip:" / "Note:" style prefixes, in any language
        tip = re.sub(
            r"^(tip|note|suggestion|ipucu|not|öneri)[:\-–]\s*",
            "",
            tip,
            flags=re.IGNORECASE,
        )
        if _is_empty_tip_signal(tip):
            logger.debug(f"[Coach.gate2] empty signal received: {tip!r}")
            return None
        if not tip or len(tip) > 250:
            return None
        logger.debug(f"[Coach.gate2] tip='{tip}'")
        return tip
    except Exception as e:
        logger.warning(f"[Coach.gate2] failed: {e}")
        return None


# Patterns the model uses to refuse a tip when it should just be silent.
# The prompt asks for the typed `<no_tip/>` token specifically; this list
# catches the meta-commentary variants the model still leaks occasionally.
_NO_TIP_TOKENS = frozenset({
    "<no_tip/>", "<no_tip>", "</no_tip>", "<notip/>",
    "no_tip", "no tip", "notip",
})

_META_COMMENTARY_RE = re.compile(
    r"^("
    r"no tips?\b|no_tip|<no_tip|"
    r"i have no|i don'?t have|"
    r"there(?:'?s| is| are) no|"
    r"nothing (to|worth)|"
    r"gate is being|gate rejected|"
    r"hiç tip|şu an için|önerim yok"
    r")",
    re.IGNORECASE,
)


def _is_empty_tip_signal(tip: str) -> bool:
    """True iff ``tip`` is a deliberate or accidental 'no tip' signal.

    Three layers, generous on purpose — model output is fuzzy and a tip
    that starts with "No tip" is never the real tip we want to show.
    """
    if not tip:
        return True
    normalised = tip.strip().lower()
    if normalised in _NO_TIP_TOKENS:
        return True
    if _META_COMMENTARY_RE.match(normalised):
        return True
    return False


async def critic(
    provider: LLMProvider,
    model: str,
    tip: str,
    conversation: str,
    user_context: str,
) -> bool:
    """Stage 3: would the user actually want this on their screen now?"""
    system = (
        "You are a ruthless critic of meeting coach tips. Decide if this tip "
        "is worth showing the user.\n\n"
        "REJECT if:\n"
        "- User already knows what's being said\n"
        "- It's a vague goal reminder without new info\n"
        "- Uses corporate buzzwords (align, prioritize, ensure, leverage)\n"
        "- Removing it would change nothing about the user's day\n"
        "- It's longer than 100 characters\n"
        "- It's not directly actionable\n\n"
        "ACCEPT only if the user would genuinely say: 'oh, glad I saw this'.\n\n"
        "Respond with ONLY a JSON object: {\"useful\": true|false, \"reason\": \"brief\"}"
    )
    user = (
        f"USER CONTEXT:\n{user_context or '(none)'}\n\n"
        f"RECENT CONVERSATION:\n{conversation}\n\n"
        f"PROPOSED TIP:\n{tip}\n\n"
        "Is this tip worth showing? Return JSON."
    )
    try:
        response = await provider.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            max_tokens=100,
            temperature=0.0,
            timeout=15.0,
            purpose="coaching",
        )
        ok = _parse_bool(response.content or "")
        logger.debug(f"[Coach.gate3] useful={ok}")
        return ok
    except Exception as e:
        logger.warning(f"[Coach.gate3] failed: {e}")
        return False


async def summarize_meeting(
    provider: LLMProvider,
    model: str,
    transcript: str,
    user_context: str,
) -> str:
    """Generate a post-meeting summary as 3-5 key takeaways the USER (not the
    other attendees) should remember long-term.

    The full transcript is kept in the meeting artifact — this summary is what
    gets appended to MEMORY.md, so it must read like notes the user would
    actually want next month, not minutes from a board meeting. Verbose
    "who/decisions/actions/numbers/questions" five-section summaries fill
    memory with runtime context that expires within days; we want the
    opposite: a small handful of long-lived points, or nothing at all when
    the meeting didn't produce any.
    """
    system = (
        "You are distilling a meeting into 3–5 KEY TAKEAWAYS for the user "
        "(the person whose memory this saves into).\n\n"
        "INCLUDE:\n"
        "- Decisions the user committed to or that change their plans\n"
        "- Action items assigned TO THE USER (not to others)\n"
        "- Numbers, dates, deadlines that the user will need to reference later\n"
        "- New durable facts about people / projects (long-lived; not 'we met on Tuesday')\n\n"
        "SKIP:\n"
        "- Catch-up / small talk\n"
        "- Action items belonging to other attendees\n"
        "- Restating what was already known going in\n"
        "- Anything that will be obsolete in a week (one-off schedules, "
        "ad-hoc questions, ephemeral status)\n\n"
        "Output 3–5 short bullets in markdown. If the meeting produced "
        "nothing worth remembering long-term, output an empty string. "
        "Be conservative — fewer, better bullets beat completeness. "
        "Don't invent details."
    )
    user = (
        f"USER CONTEXT:\n{user_context or '(none)'}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\n"
        "Key takeaways:"
    )
    try:
        response = await provider.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            max_tokens=250,
            temperature=0.3,
            timeout=60.0,
            purpose="coaching",
        )
        return (response.content or "").strip()
    except Exception as e:
        logger.warning(f"[Coach.summarize] failed: {e}")
        return ""


# Identity-level predicates we KEEP in the KG. These describe who someone is
# or how to reach them — durable facts that stay true between meetings.
#
# Anything outside this set (met_on, agreed_on, deadline, budget, discussed,
# attended, said, decided, mentioned, ...) is meeting-specific runtime
# context. That kind of fact belongs in the meeting artifact / memory
# takeaways, not the KG, because (a) it expires within days and (b) it
# turns the graph into a noisy event log instead of a relationship store.
KG_ALLOWED_PREDICATES: set[str] = {
    "email", "phone",
    "role", "title",
    "works_at", "works_with", "manages", "reports_to",
    "lives_in", "based_in",
    "uses", "owns", "prefers",
}


async def extract_entities(
    provider: LLMProvider,
    model: str,
    summary: str,
) -> list[dict[str, Any]]:
    """Extract KG-ready entity triples from a meeting summary.

    Two-layer noise control:
    1. The prompt restricts the LLM to identity-level facts (who someone is,
       how to reach them, structural relationships). Temporal context like
       "met_on" or "deadline" is explicitly excluded — those belong in the
       meeting artifact, not the relationship graph.
    2. A code-side whitelist (KG_ALLOWED_PREDICATES) drops anything the
       model still slips through. Defense in depth so a single jailbreak in
       the prompt can't pollute the graph.
    """
    system = (
        "Extract IDENTITY-LEVEL facts from a meeting summary as triples for "
        "a knowledge graph. The graph stores durable relationships that stay "
        "true between meetings — NOT a log of what happened in this one.\n\n"
        "ONLY extract facts that fit these predicates:\n"
        "- email, phone (contact info)\n"
        "- role, title (what someone's job is)\n"
        "- works_at, works_with, manages, reports_to (org relationships)\n"
        "- lives_in, based_in (location)\n"
        "- uses, owns, prefers (tools/products durably associated with a person/team)\n\n"
        "DO NOT extract:\n"
        "- met_on, agreed_on, attended, said, discussed, decided, mentioned "
        "(meeting-specific events)\n"
        "- deadline, budget, schedule (numbers that expire)\n"
        "- restated facts that the user already knows (e.g. their own email)\n"
        "- vague aliases (always use the full name from the summary)\n\n"
        "Return a JSON array of objects, each with:\n"
        "- subject: full name (e.g. 'Ahmet Yılmaz', not 'Ahmet')\n"
        "- predicate: one of the allowed predicates above, snake_case\n"
        "- object: the value or other entity\n"
        "- subject_type: person|company|project|event\n"
        "- object_type: person|company|project|event|\"\" (empty when object is a literal value)\n\n"
        "Output ONLY a valid JSON array — no prose. When in doubt, return [] — "
        "empty is the right answer most of the time. Most meetings produce "
        "zero identity-level facts."
    )
    user = f"SUMMARY:\n{summary}\n\nJSON array:"
    try:
        response = await provider.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            max_tokens=400,
            temperature=0.0,
            timeout=45.0,
            purpose="coaching",
        )
        text = (response.content or "").strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        entities = json.loads(text)
        if not isinstance(entities, list):
            return []
        # Validate shape + apply predicate whitelist. The whitelist is the
        # last line of defense — even if the prompt fails to keep the model
        # on-rails, off-list predicates are silently dropped here so the
        # graph stays clean.
        valid: list[dict[str, Any]] = []
        for e in entities:
            if not isinstance(e, dict):
                continue
            if not all(k in e for k in ("subject", "predicate", "object")):
                continue
            predicate = str(e.get("predicate", "")).strip().lower()
            if predicate not in KG_ALLOWED_PREDICATES:
                logger.debug(f"[Coach.extract_entities] dropped off-list predicate '{predicate}'")
                continue
            # Empty subject/object never makes a useful triple.
            if not str(e.get("subject", "")).strip() or not str(e.get("object", "")).strip():
                continue
            valid.append(e)
        return valid
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[Coach.extract_entities] parse failed: {e}")
        return []
    except Exception as e:
        logger.warning(f"[Coach.extract_entities] failed: {e}")
        return []


