"""Step validator — separate Haiku LLM call to check evidence vs criteria.

This is the killer feature from the research findings. Skyvern's
85.8% WebVoyager score and the Reflexion follow-up paper converge:
LLMs cannot self-correct without external verification signals.
A model marking its own work "done" is unreliable; a separate
small LLM with one job ("does this evidence match this criteria?")
is dramatically more reliable.

Design:
- Inputs: step (with successCriteria), evidence string, optional
  screenshot data URL.
- Output: ValidatorResult with matches: bool, reason: str,
  suggested_fix: str.
- Cost: one Haiku 4.5 call per `update_step(done)` — typically
  ~600 tokens, ~500ms. Cheap enough to run on every step.
- Failure modes: provider unreachable, malformed JSON response,
  timeout. All degrade gracefully (return matches=None) so the
  step still marks done — we never block agent progress on a
  validator infrastructure failure. Logs warn so operators know.

Phase 2: integrated into browser_plan.update_step. Phase 3 will
add validator chains (planner re-plan on repeated fails) and
optional vision (pass current screenshot to validator).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from flowly.agent.planner.models import Step


# Smaller, faster, cheaper model for the validator. We deliberately
# don't use the same model the agent runs on — separation of
# concerns is the whole point.
VALIDATOR_MODEL = "anthropic/claude-haiku-4.5"
VALIDATOR_TIMEOUT_S = 12.0
VALIDATOR_MAX_TOKENS = 400


@dataclass
class ValidatorResult:
    """Outcome of a single validator pass.

    `matches=None` is reserved for "validator infra failed" (timeout,
    no provider, bad JSON). The caller treats it as a soft pass
    (don't block) and emits a log warning. `matches=True/False` are
    real verdicts.
    """

    matches: Optional[bool]
    reason: str
    suggested_fix: Optional[str] = None
    raw_response: Optional[str] = None
    via: str = "llm"  # llm | infra_fail | disabled

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": self.matches,
            "reason": self.reason,
            "suggested_fix": self.suggested_fix,
            "via": self.via,
        }

    @classmethod
    def infra_fail(cls, reason: str) -> "ValidatorResult":
        return cls(matches=None, reason=reason, via="infra_fail")

    @classmethod
    def disabled(cls, reason: str = "validator disabled") -> "ValidatorResult":
        return cls(matches=None, reason=reason, via="disabled")


_SYSTEM_PROMPT = (
    "You are a precise verification model. Your ONLY job is to decide "
    "whether a piece of evidence demonstrates that a step's success "
    "criteria has been satisfied. Reply with strict JSON, no prose.\n\n"
    "OUTPUT SCHEMA:\n"
    '{"matches": true|false, "reason": "<one sentence>", '
    '"suggested_fix": "<one sentence or null>"}\n\n'
    "RULES:\n"
    "- matches=true ONLY if the evidence concretely demonstrates the "
    "criteria. Vague matches like 'I think it worked' are matches=false.\n"
    "- matches=false if evidence is missing, contradicts the criteria, "
    "is generic, or simply restates the action without proof.\n"
    "- reason: ONE sentence explaining the verdict. Quote the evidence/"
    "criteria where useful.\n"
    "- suggested_fix (only when matches=false): ONE sentence telling "
    "the actor what would constitute valid evidence (e.g. 'screenshot "
    "the sidebar and confirm the formula text appears').\n"
    "- Do NOT add prose before or after the JSON. Do NOT use markdown.\n"
    "- If the evidence and criteria look perfectly aligned, you should "
    "still set matches=true — don't be paranoid. Your job is honest "
    "verification, not adversarial review."
)


def _build_user_prompt(step: Step, evidence: str) -> str:
    return (
        f"STEP: {step.content}\n"
        f"SUCCESS CRITERIA: {step.successCriteria}\n"
        f"CLAIMED EVIDENCE: {evidence}\n\n"
        f"Does the evidence demonstrate the criteria are satisfied? "
        f"Reply with JSON only."
    )


def _parse_response(raw: str) -> Optional[dict[str, Any]]:
    """Extract a JSON object from the model output.

    Tolerates code fences and surrounding prose — the prompt asks for
    pure JSON but real-world LLMs sometimes wrap. We grab the first
    balanced {...} block.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            parts = text.split("\n", 1)
            text = parts[1] if len(parts) > 1 else ""
    # Direct parse first
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fallback: find first {...} balanced block
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = text[start : i + 1]
                try:
                    return json.loads(snippet)
                except Exception:
                    return None
    return None


async def validate_step(
    step: Step,
    evidence: str,
    provider: Any,
) -> ValidatorResult:
    """Run one validator pass.

    Returns a ValidatorResult. NEVER raises — infra failures degrade
    to ValidatorResult.infra_fail() so the caller can decide policy
    (we soft-pass to keep the agent moving).
    """
    if provider is None:
        return ValidatorResult.disabled("no provider wired")

    if not evidence or not evidence.strip():
        # Caller should already gate on this, but be defensive.
        return ValidatorResult(
            matches=False,
            reason="No evidence provided.",
            suggested_fix="Describe what you observed (screenshot/DOM/URL change).",
            via="llm",
        )

    user_prompt = _build_user_prompt(step, evidence)
    try:
        response = await asyncio.wait_for(
            provider.chat(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                model=VALIDATOR_MODEL,
                max_tokens=VALIDATOR_MAX_TOKENS,
                temperature=0.0,
            ),
            timeout=VALIDATOR_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[validator] timeout after {VALIDATOR_TIMEOUT_S}s on step {step.id} — soft-passing"
        )
        return ValidatorResult.infra_fail(f"validator LLM timed out after {VALIDATOR_TIMEOUT_S}s")
    except Exception as e:
        logger.warning(f"[validator] LLM call failed: {e} — soft-passing")
        return ValidatorResult.infra_fail(f"validator LLM call failed: {e}")

    raw = (getattr(response, "content", "") or "").strip()
    parsed = _parse_response(raw)
    if not isinstance(parsed, dict):
        logger.warning(
            f"[validator] non-JSON response on step {step.id}: {raw[:120]!r} — soft-passing"
        )
        return ValidatorResult.infra_fail(f"validator returned non-JSON: {raw[:120]!r}")

    matches = parsed.get("matches")
    if matches is not True and matches is not False:
        return ValidatorResult.infra_fail(
            f"validator JSON missing matches bool: {parsed!r}"
        )
    reason = str(parsed.get("reason") or "(no reason given)").strip()
    suggested_fix = parsed.get("suggested_fix")
    if suggested_fix:
        suggested_fix = str(suggested_fix).strip() or None
    return ValidatorResult(
        matches=matches,
        reason=reason[:300],
        suggested_fix=(str(suggested_fix)[:300] if suggested_fix else None),
        raw_response=raw[:500],
    )
