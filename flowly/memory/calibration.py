"""Confidence calibration — replaces the raw, unreliable LLM-reported number.

An extractor's self-reported confidence is noisy. Instead we *compute* a
calibrated score from observable signals (plan §3):

* whether the user stated the fact explicitly vs the agent inferred it;
* cross-session repetition (the same fact recurring is stronger evidence);
* recency, with decay for time-bound facts only;
* whether it contradicts an existing active fact.

The defaults are tuned so a single explicitly-stated, unconflicted fact clears
``auto_floor`` (0.80) while an inferred-once fact lands in the needs_review band
(``review_floor`` ≤ x < ``auto_floor``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationWeights:
    base: float = 0.60           # a fact worth extracting starts mid-scale
    explicit_bonus: float = 0.25  # user literally said it
    repeat_step: float = 0.05     # per extra sighting
    repeat_cap: float = 0.15      # max repetition contribution
    conflict_penalty: float = 0.30
    half_life_days: float = 30.0  # decay half-life for temporal facts


DEFAULT_WEIGHTS = CalibrationWeights()


def calibrate(
    *,
    is_explicit: bool,
    seen_count: int = 1,
    age_days: float = 0.0,
    had_conflict: bool = False,
    temporal: bool = False,
    weights: CalibrationWeights = DEFAULT_WEIGHTS,
) -> float:
    """Return a calibrated confidence in [0, 1].

    ``seen_count`` is the total number of times this fact (by key) has been
    observed, including the current sighting. ``temporal`` enables recency decay
    (off for profile/preference facts, which don't go stale with time).
    """
    score = weights.base
    if is_explicit:
        score += weights.explicit_bonus
    score += min(weights.repeat_cap, weights.repeat_step * max(0, seen_count - 1))
    if temporal:
        score *= 0.5 ** (max(0.0, age_days) / weights.half_life_days)
    if had_conflict:
        score -= weights.conflict_penalty
    return max(0.0, min(1.0, score))
