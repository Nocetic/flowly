"""Auto-generate a short, descriptive session title from the first exchange.

After the first
user→assistant turn, a cheap LLM call turns the opening exchange into a 3-7
word title, stored on ``session.metadata['title']`` so EVERY client — CLI,
desktop, iOS — shows the SAME descriptive name instead of a random session-key
suffix ("MQ79RSLB8Y5NJ5" on desktop, "62AF06DA" on iOS, …). Fire-and-forget;
never blocks the user-facing reply.
"""

from __future__ import annotations

import re

from loguru import logger

# Reasoning models (e.g. stepfun/step-*-flash, deepseek-r1) emit their chain of
# thought inline, wrapped in tags, before the visible answer. Strip those blocks
# so the title isn't a stray line of reasoning.
_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that "
    "starts with the following exchange. Capture the main topic or intent. "
    "Write the title in the SAME language as the user's message. "
    "Return ONLY the title text — no quotes, no trailing punctuation, no "
    "prefixes like 'Title:'."
)


async def generate_title(
    provider,
    model: str | None,
    user_message: str,
    assistant_response: str,
    timeout: float = 20.0,
) -> str | None:
    """Return a short title for the opening exchange, or None on failure.

    Uses the agent's own provider/model with a tiny token budget. Truncates the
    inputs so the request stays cheap.
    """
    user_snippet = (user_message or "")[:500].strip()
    asst_snippet = (assistant_response or "")[:500].strip()
    if not user_snippet:
        return None

    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {"role": "user", "content": f"User: {user_snippet}\n\nAssistant: {asst_snippet}"},
    ]

    try:
        # max_tokens is deliberately large so this works on ANY model. Reasoning
        # models (stepfun/step-*-flash, deepseek-r1, …) spend tokens on hidden
        # chain-of-thought BEFORE the visible answer; a tight budget gets fully
        # consumed by reasoning and the content comes back EMPTY (raw_len=0 —
        # the "generator returned no title" failure we saw on step-3.7-flash
        # even at 512). Give them enough room to finish thinking and still emit
        # the title. Non-reasoning models stop after a few tokens, so the high
        # ceiling costs them nothing; we only ever keep the first clean line.
        resp = await provider.chat(
            messages=messages,
            model=model,
            max_tokens=2048,
            temperature=0.3,
            timeout=timeout,
            purpose="title",
        )
        # Providers return errors as a normal LLMResponse (content="Error
        # calling LLM: …", finish_reason="error") rather than raising — never
        # turn that into a chat title.
        if getattr(resp, "finish_reason", None) == "error":
            logger.warning(f"[title] provider error response for model={model!r}; skipping title")
            return None
        raw = (getattr(resp, "content", "") or "")
        if raw.startswith("Error calling LLM:"):
            logger.warning(f"[title] error-shaped content for model={model!r}; skipping title")
            return None
        # Drop inline reasoning blocks before any other parsing.
        title = _THINK_BLOCK.sub("", raw).strip()
        # Clean up: strip surrounding quotes, a leading "Title:" prefix, and
        # cap the length so a chatty model can't produce a paragraph.
        title = title.strip('"').strip("'").strip()
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        # Some models wrap reasoning in newlines — keep the first non-empty line.
        first_line = next((ln.strip() for ln in title.splitlines() if ln.strip()), "")
        title = first_line or title
        if len(title) > 80:
            title = title[:77] + "..."
        if not title:
            # Distinguish "model returned nothing usable" from an exception so an
            # operator can tell a budget/model problem from a transport error.
            logger.warning(
                f"[title] empty title from model={model!r} "
                f"(raw_len={len(raw)}); content may be reasoning-only"
            )
        return title or None
    except Exception as e:
        logger.warning(f"[title] generation failed for model={model!r}: {e!r}")
        return None
