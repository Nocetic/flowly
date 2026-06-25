"""Real-LLM regression tests for the coaching gate.

These tests hit the live OpenRouter API. They are intentionally skipped by
default to keep `pytest` fast and free; run them explicitly when you want
to validate gate behaviour after touching prompts:

    pytest tests/test_coaching_gate_real.py -m real_llm -v

Credentials are resolved in this order:
    1. OPENROUTER_API_KEY / OPENROUTER_API_BASE env vars (CI-friendly)
    2. ~/.flowly/config.json providers.openrouter.{apiKey,apiBase}
       (works out of the box on a developer machine that already runs
       Flowly locally)

If neither yields a key, the test is skipped with a clear message.

Why this exists
---------------
The existing coaching tests mock the LLM entirely, so prompt changes ship
without anyone noticing behavioural drift. These fixtures pin the gate's
score envelope for known inputs (Turkish debate, small talk, factual
questions, etc.) so we can iterate on the prompt and see, in numbers,
what got better and what got worse.

Each fixture asserts a score *range* (not an exact value) — LLMs have
variance even at temperature 0.0, and tightening to a single number
would just produce flaky tests.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from flowly.coaching.gate import relevance_gate
from flowly.providers.openrouter_provider import OpenRouterProvider

# ── Configuration ─────────────────────────────────────────────────────────────

GATE_MODEL = "anthropic/claude-haiku-4.5"
"""Model used by production coaching gate. Keep this in sync with
flowly/coaching/manager.py:gate_model so the test pins prod behaviour."""

# Default proactive threshold (gate.py:FREQUENCY_THRESHOLDS["proactive"]).
# We pass the threshold to relevance_gate() but assert against raw score, so
# the threshold here is informational — it shows what would *actually* fire
# in proactive mode for each fixture.
PROACTIVE_THRESHOLD = 0.40


# ── Credential resolution ────────────────────────────────────────────────────


def _resolve_credentials() -> tuple[str | None, str | None]:
    """Return (api_key, api_base) from env or ~/.flowly/config.json.

    Both can be None if no source has them; caller decides whether to skip.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY") or ""
    api_base = os.environ.get("OPENROUTER_API_BASE") or ""

    if api_key and api_base:
        return api_key, api_base

    config_path = Path.home() / ".flowly" / "config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            openrouter = data.get("providers", {}).get("openrouter", {})
            api_key = api_key or openrouter.get("apiKey", "") or ""
            api_base = api_base or openrouter.get("apiBase", "") or ""
        except (OSError, json.JSONDecodeError):
            pass  # fall through; pytest will skip if creds incomplete

    return (api_key or None, api_base or None)


# ── Pytest config ─────────────────────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """Auto-skip real_llm tests when no credentials are resolvable."""
    api_key, _ = _resolve_credentials()
    if api_key:
        return
    skip_marker = pytest.mark.skip(
        reason=(
            "No OpenRouter credentials. Set OPENROUTER_API_KEY env var or "
            "configure providers.openrouter.apiKey in ~/.flowly/config.json"
        )
    )
    for item in items:
        if "real_llm" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(scope="module")
def llm_provider() -> OpenRouterProvider:
    api_key, api_base = _resolve_credentials()
    if not api_key:
        pytest.skip("No OpenRouter credentials available")
    return OpenRouterProvider(api_key=api_key, api_base=api_base or None)


# ── Fixture data ──────────────────────────────────────────────────────────────


@dataclass
class GateFixture:
    """One scoring scenario with expected score envelope."""

    name: str
    user_context: str
    conversation: str
    expected_min: float
    expected_max: float
    rationale: str
    """Why this score range — keeps the intent of each fixture documented."""


# Bug case + healthy cases + low-score (silence) cases.
#
# Score envelopes are intentionally generous (0.20 wide) because:
#   - Gate uses a small/cheap model (haiku-4.5) that has run-to-run drift
#   - Prompt iteration may legitimately shift scores ±0.10 without changing
#     the qualitative outcome (still passes/rejects at the same threshold)
#
# If a fixture starts failing after a prompt change, that's the signal —
# investigate before merging.

FIXTURES: list[GateFixture] = [
    # ─── BUG CASE: substantive debate WITH explicit user intent ───────────────
    GateFixture(
        name="tr_religious_debate_with_commentary_intent",
        user_context=(
            "İki kişinin tartışmasını dinle, kendi yorumlarını ve "
            "çıkarımlarını sun. Karşı çıkmak olabilir, argüman desteklemek "
            "olabilir."
        ),
        conversation=(
            "Ölünüzde yine sonsuz mudur? Ya bu kâinatta iki yaratıcı nasıl "
            "olur da o sonsuz iradeyi paylaşır? O zaman bu kabul edilmek "
            "zorundadır. Görülmeyen, duyulmayan, hiçbir şeye benzemeyen "
            "bir varlık. Kitabın konusu da agnostisizm ve ilahi tragedya idi. "
            "Aslında burada konuşacağımız temel soru şu: bu sistem ne için "
            "kurulmuş? Ne için bana hizmet ediyor?"
        ),
        expected_min=0.50,
        expected_max=0.95,
        rationale=(
            "User explicitly asked for commentary on debates. Substantive "
            "philosophical discussion → assistant should weigh in with "
            "perspective, fact, or counter-argument."
        ),
    ),
    # ─── Same content WITHOUT explicit intent — should be lower but not zero ─
    GateFixture(
        name="tr_religious_debate_no_context",
        user_context="",
        conversation=(
            "Ölünüzde yine sonsuz mudur? Ya bu kâinatta iki yaratıcı nasıl "
            "olur da o sonsuz iradeyi paylaşır? O zaman bu kabul edilmek "
            "zorundadır."
        ),
        expected_min=0.20,
        expected_max=0.65,
        rationale=(
            "Without explicit user intent, philosophical debate is "
            "borderline — either side acceptable. Not a clear LOW (0.15) "
            "because terms ARE worth explaining (Priority 2 default rules)."
        ),
    ),
    # ─── Clear LOW: small talk should always score low ────────────────────────
    GateFixture(
        name="tr_small_talk",
        user_context="",
        conversation=(
            "Nasılsın? İyiyim teşekkürler, sen nasılsın? "
            "Ben de iyiyim. Kahve içelim mi? Olur, hadi gidelim."
        ),
        expected_min=0.0,
        expected_max=0.30,
        rationale="Pure greeting / filler. Default rules → LOW (≤0.30).",
    ),
    GateFixture(
        name="en_small_talk",
        user_context="",
        conversation=(
            "How was your weekend? Good, thanks. Yours? "
            "Not bad. Want some coffee? Yeah, sounds great."
        ),
        expected_min=0.0,
        expected_max=0.30,
        rationale="English equivalent of the above — filler / greetings.",
    ),
    # ─── Clear HIGH: factual question with a concrete answer ──────────────────
    GateFixture(
        name="tr_factual_question",
        user_context="",
        conversation=(
            "Bu blockchain dediğimiz şey tam olarak ne? Ben hiç "
            "anlamadım açıkçası. Birisi bana basit bir şekilde anlatabilir mi?"
        ),
        expected_min=0.60,
        expected_max=1.0,
        rationale=(
            "User explicitly said 'I don't understand' AND asked a factual "
            "question with a clear answer. Default rule → SCORE HIGH."
        ),
    ),
    GateFixture(
        name="en_decision_point",
        user_context="",
        conversation=(
            "OK, so we need to decide on the Q1 budget. I'm proposing $5 "
            "million for marketing. That feels high — we did $3M last year "
            "and barely broke even. Are we sure $5M is the right number?"
        ),
        expected_min=0.55,
        expected_max=1.0,
        rationale=(
            "Decision point with a concrete number being challenged. "
            "Default rule → HIGH."
        ),
    ),
    # ─── Lecture / learning context ───────────────────────────────────────────
    GateFixture(
        name="tr_lecture_with_learning_intent",
        user_context=(
            "Bu bir ders, anlamadığım terimleri ve kavramları açıkla."
        ),
        conversation=(
            "Yapay sinir ağlarında geri yayılım algoritması, ağırlıkları "
            "gradient descent ile günceller. Bu işlem chain rule "
            "kullanılarak her katmanda tekrar tekrar hesaplanır. "
            "Convolutional layer'larda ise filtreler paylaşılır."
        ),
        expected_min=0.55,
        expected_max=1.0,
        rationale=(
            "User wants concept explanations. Transcript dense with "
            "technical terms (geri yayılım, gradient descent, chain rule, "
            "convolutional). HIGH score expected."
        ),
    ),
    # ─── Sales / guardian scenario ────────────────────────────────────────────
    GateFixture(
        name="en_sales_with_guardian_intent",
        user_context=(
            "I'm in a customer call. Stop me if I'm about to commit to "
            "wrong numbers or bad terms."
        ),
        conversation=(
            "OK, so for the enterprise tier we can do unlimited seats at "
            "$50 per seat per month. Yeah, that sounds great, let's lock "
            "that in for the three-year contract."
        ),
        expected_min=0.55,
        expected_max=1.0,
        rationale=(
            "User explicitly asked for guardian-mode interruption. The "
            "speaker just committed to a 3-year unlimited-seat deal at a "
            "specific price — exactly what user wants caught."
        ),
    ),
    # ─── Mid-thought: should NOT interrupt ────────────────────────────────────
    GateFixture(
        name="en_mid_thought",
        user_context="",
        conversation=(
            "I think we should... uh... maybe consider... no wait, "
            "let me think about this differently. So if we... hmm."
        ),
        expected_min=0.0,
        expected_max=0.40,
        rationale=(
            "Mid-thought, speaker working it out. Default rule explicitly "
            "says SCORE LOW for 'mid-thought — wait for natural pause'."
        ),
    ),
    # ─── Silent-mode user context ─────────────────────────────────────────────
    GateFixture(
        name="en_silent_mode_context",
        user_context=(
            "I'm on a sensitive 1-on-1. Stay silent unless someone shares "
            "factually wrong information."
        ),
        conversation=(
            "I just feel like the team isn't connecting lately. The Monday "
            "standups feel mechanical and I miss the energy we used to have "
            "when we were in the office together."
        ),
        expected_min=0.0,
        expected_max=0.35,
        rationale=(
            "User asked for guardian/silent mode. Conversation is emotional "
            "/subjective with no factual claims to correct. Should be quiet."
        ),
    ),
]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.real_llm
@pytest.mark.parametrize(
    "fixture",
    FIXTURES,
    ids=[f.name for f in FIXTURES],
)
async def test_gate_score_in_expected_range(
    llm_provider: OpenRouterProvider,
    fixture: GateFixture,
) -> None:
    """For each fixture, the gate's relevance score must fall in the
    expected envelope. This is the regression contract: a prompt change
    that breaks one of these IS a behaviour change."""
    passed, score, reason = await relevance_gate(
        provider=llm_provider,
        model=GATE_MODEL,
        conversation=fixture.conversation,
        user_context=fixture.user_context,
        kg_context="",
        threshold=PROACTIVE_THRESHOLD,
        mode="assistant",
    )

    # Diagnostic print — only shown on failure or with -v
    print(
        f"\n[{fixture.name}] score={score:.2f} "
        f"(expected {fixture.expected_min:.2f}–{fixture.expected_max:.2f}) "
        f"passed={passed} reason={reason!r}"
    )

    assert fixture.expected_min <= score <= fixture.expected_max, (
        f"\nFixture: {fixture.name}\n"
        f"Score {score:.2f} outside expected {fixture.expected_min:.2f}"
        f"–{fixture.expected_max:.2f}\n"
        f"LLM reason: {reason!r}\n"
        f"Rationale for envelope: {fixture.rationale}\n"
    )


@pytest.mark.real_llm
async def test_reason_field_populated(
    llm_provider: OpenRouterProvider,
) -> None:
    """The gate prompt requires a 'reason' field. Empty reason is a known
    failure mode — the prompt should be strict enough that LLM always
    returns one."""
    fixture = FIXTURES[0]  # any fixture works
    _, _, reason = await relevance_gate(
        provider=llm_provider,
        model=GATE_MODEL,
        conversation=fixture.conversation,
        user_context=fixture.user_context,
        kg_context="",
        threshold=PROACTIVE_THRESHOLD,
        mode="assistant",
    )
    assert reason, (
        "LLM returned empty reason field. Gate prompt should require it. "
        "If this regresses, the JSON output spec needs tightening."
    )
    assert len(reason) >= 3, f"Reason too short: {reason!r}"


@pytest.mark.real_llm
async def test_gate_scores_lower_when_topic_already_covered(
    llm_provider: OpenRouterProvider,
) -> None:
    """The gate must consult RECENT TIPS YOU ALREADY GAVE and score lower
    when the only useful thing to say overlaps an already-emitted tip.

    Regression target — users reported "Coach repeats itself" on continuing
    conversations. This test passes the same conversation twice; once with
    an empty tip history, once with a tip that already covers the same
    angle. The second score must drop meaningfully — otherwise the model
    is ignoring the recent_tips section entirely.
    """
    # A clear-cut decision-point conversation. Without a recent tip, the
    # gate consistently scores it 0.70+ (verified across runs).
    conversation = (
        "OK, so we need to decide on the Q1 budget. I'm proposing $5 "
        "million for marketing. That feels high — we did $3M last year "
        "and barely broke even. Are we sure $5M is the right number?"
    )

    # First call — clean slate, expect a normal high score.
    _, score_no_tips, reason_no_tips = await relevance_gate(
        provider=llm_provider,
        model=GATE_MODEL,
        conversation=conversation,
        user_context="",
        kg_context="",
        threshold=PROACTIVE_THRESHOLD,
        mode="assistant",
        recent_tips=[],
    )

    # Second call — pretend we already gave a tip on this exact angle.
    already_said = (
        "Validate the $5M against last year's $3M and ROI before committing"
    )
    _, score_with_tips, reason_with_tips = await relevance_gate(
        provider=llm_provider,
        model=GATE_MODEL,
        conversation=conversation,
        user_context="",
        kg_context="",
        threshold=PROACTIVE_THRESHOLD,
        mode="assistant",
        recent_tips=[already_said],
    )

    print(
        f"\n  no recent tips → score={score_no_tips:.2f} reason={reason_no_tips!r}"
        f"\n  with overlap   → score={score_with_tips:.2f} reason={reason_with_tips!r}"
    )

    # The drop must be meaningful (not just noise within LLM variance).
    # 0.15 is a conservative threshold; in practice we see drops of 0.30+.
    assert score_with_tips < score_no_tips - 0.15, (
        f"Score did not drop enough when conversation overlaps a recent tip.\n"
        f"  Without tips: {score_no_tips:.2f} ({reason_no_tips!r})\n"
        f"  With tip:     {score_with_tips:.2f} ({reason_with_tips!r})\n"
        f"Either the prompt's anti-repetition rule isn't being followed,\n"
        f"or the recent_tips param isn't reaching the LLM."
    )
