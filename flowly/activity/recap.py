"""The owner-facing words for a finished task, written by the bot's own model.

One request per task, after it ended and off the reply's path. The model sees
a compact account of the task — what was asked, each step's kind, target,
success and the start of its result, the start of the reply — never the whole
conversation, so the cost is a few thousand tokens and does not grow with the
chat. Whatever comes back is validated and cleaned; anything unusable leaves
the task with no summary rather than a wrong one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

MAX_STEPS_IN_PROMPT = 20
STEP_EXCERPT_CHARS = 1_500
REPLY_EXCERPT_CHARS = 2_000
TIMEOUT_SECONDS = 30.0
TITLE_MAX = 80
OUTCOME_MAX = 140
SUMMARY_MAX = 600
NOTE_MAX = 200

_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
# Citation debris some models leave in prose: 【12345†L11-L13】, [^1], ‡.
_CITATIONS = re.compile(r"【[^】]*】|\[\^\d+\]|[†‡]")
_MARKDOWN = re.compile(r"(\*\*|__|`|^#+\s*)", re.MULTILINE)

_SYSTEM = (
    "You write the activity log of an AI agent for its owner. You get one finished task: what "
    "was asked, the steps the agent took and the start of its reply. Return ONLY a JSON object, "
    "no prose around it:\n"
    '{"title": "...", "outcome": "...", "summary": "...", "steps": [{"i": 0, "note": "..."}]}\n'
    "- title: at most 6 words, imperative, names the task (\"Research Hermes AI agent\").\n"
    "- outcome: at most 12 words, past tense, what came of it.\n"
    "- summary: 1-3 sentences in the first person, as the agent.\n"
    "- steps: one short line per listed step, what it found or did; use the step's index as i.\n"
    "Write in the same language as the request. Plain text: no markdown, no citations, no "
    "quotes around values. Never include secrets, keys or full file contents."
)


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _CITATIONS.sub("", value)
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"\s+", " ", text).strip().strip("\"'“”‘’").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_messages(record: dict[str, Any], excerpts: list[str], reply: str) -> list[dict[str, str]]:
    lines = [f"Request: {record.get('request') or '(none)'}"]
    trigger = record.get("trigger") or {}
    if trigger.get("kind") and trigger.get("kind") != "owner":
        lines.append(f"Started by: {trigger.get('kind')}")
    steps = record.get("steps") or []
    lines.append(f"Status: {record.get('status')}")
    lines.append("Steps:")
    for index, step in enumerate(steps[:MAX_STEPS_IN_PROMPT]):
        # Capped here too: the request's size must not depend on the caller.
        excerpt = (excerpts[index] or "")[:STEP_EXCERPT_CHARS] if index < len(excerpts) else ""
        target = f" {step.get('target')}" if step.get("target") else ""
        outcome = "ok" if step.get("ok") else ("blocked" if step.get("blocked") else "failed")
        lines.append(f"[{index}] {step.get('tool')} ({step.get('kind')}){target} -> {outcome}")
        if excerpt.strip():
            lines.append(f"    result: {excerpt.strip()}")
    if len(steps) > MAX_STEPS_IN_PROMPT:
        lines.append(f"(+{len(steps) - MAX_STEPS_IN_PROMPT} more steps)")
    if not steps:
        lines.append("(no tools used)")
    lines.append(f"Reply (start): {(reply or '')[:REPLY_EXCERPT_CHARS].strip() or '(none)'}")
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse_recap(raw: Any, step_count: int) -> dict[str, Any] | None:
    """The validated recap, or None when the model's answer is unusable."""
    if not isinstance(raw, str):
        return None
    text = _FENCE.sub("", _THINK.sub("", raw).strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    title = _clean(value.get("title"), TITLE_MAX)
    outcome = _clean(value.get("outcome"), OUTCOME_MAX)
    if not title or not outcome:
        return None
    notes: dict[int, str] = {}
    for item in value.get("steps") if isinstance(value.get("steps"), list) else []:
        if not isinstance(item, dict):
            continue
        index = item.get("i")
        note = _clean(item.get("note"), NOTE_MAX)
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < min(step_count, MAX_STEPS_IN_PROMPT) and note:
            notes.setdefault(index, note)
    return {
        "title": title,
        "outcome": outcome,
        "summary": _clean(value.get("summary"), SUMMARY_MAX),
        "steps": [{"i": index, "note": note} for index, note in sorted(notes.items())],
    }


async def summarize(provider: Any, model: str | None, record: dict[str, Any],
                    excerpts: list[str], reply: str) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """``(recap or None, token usage of this request)``. Never raises."""
    try:
        response = await provider.chat(
            messages=build_messages(record, excerpts, reply),
            model=model,
            max_tokens=2048,
            temperature=0.2,
            timeout=TIMEOUT_SECONDS,
            purpose="activity_recap",
        )
    except Exception as exc:  # noqa: BLE001 — a summary is never worth an error
        logger.debug(f"[activity] recap request failed: {exc!r}")
        return None, {}
    from flowly.activity.recorder import _tokens

    usage = _tokens(getattr(response, "usage", None))
    content = getattr(response, "content", "") or ""
    if getattr(response, "finish_reason", None) == "error" or content.startswith("Error calling LLM:"):
        return None, usage
    recap = parse_recap(content, len(record.get("steps") or []))
    if recap is None:
        logger.debug("[activity] recap answer was not usable")
    return recap, usage
