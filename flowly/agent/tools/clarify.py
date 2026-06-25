"""Clarify tool — let the agent ask the user a focused question.

The agent calls this when a request is ambiguous and proceeding would
require guessing. It can offer a short list of choices or ask open-ended;
either way the user's reply comes back as free text and the agent resumes.

Surfaces always add their own "type your own answer" affordance, so the
choices list should contain only the genuine options — never a catch-all
like "Other" or "Something else".
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.clarify.manager import get_clarify_manager
from flowly.clarify.types import ClarifyRequest, MAX_CHOICES


# How long the agent waits for an answer before giving up and proceeding
# on its own judgement. Kept generous — the user may be away from the
# surface when the question arrives.
DEFAULT_TIMEOUT_SECONDS = 300


class ClarifyTool(Tool):
    @property
    def name(self) -> str:
        return "clarify"

    @property
    def description(self) -> str:
        return (
            "Ask the user a focused question when their request is ambiguous "
            "and proceeding would mean guessing. Optionally offer up to "
            f"{MAX_CHOICES} concrete choices. Do NOT add a catch-all option "
            "like 'Other' — the user can always type a free-form answer. "
            "Use this sparingly: only when the ambiguity genuinely blocks you, "
            "not for confirmation of work you can reasonably infer."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user. Be specific and concise.",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CHOICES,
                    "description": (
                        "Optional list of concrete options to offer "
                        f"(at most {MAX_CHOICES}). Omit for an open-ended question."
                    ),
                },
            },
            "required": ["question"],
        }

    async def execute(self, **kwargs: Any) -> str:
        question = (kwargs.get("question") or "").strip()
        if not question:
            return "Error: question is required."

        raw_choices = kwargs.get("choices")
        choices: list[str] | None = None
        if isinstance(raw_choices, list):
            cleaned = [str(c).strip() for c in raw_choices if str(c).strip()]
            if cleaned:
                choices = cleaned[:MAX_CHOICES]

        session_key = kwargs.get("session_key") or ""

        now = time.time()
        pending = ClarifyRequest(
            id=secrets.token_hex(8),
            question=question,
            choices=choices,
            session_key=session_key,
            created_at=now,
            expires_at=now + DEFAULT_TIMEOUT_SECONDS,
        )

        manager = get_clarify_manager()
        try:
            answer = await manager.request_and_wait(pending)
        except Exception as e:
            logger.error(f"[Clarify] request failed: {e}")
            return json.dumps({"error": f"clarify failed: {e}"})

        if answer is None:
            return json.dumps({
                "question": question,
                "choices_offered": choices,
                "user_response": None,
                "note": "No answer (timed out or unavailable). Proceed on your best judgement.",
            })

        return json.dumps({
            "question": question,
            "choices_offered": choices,
            "user_response": answer,
        })
