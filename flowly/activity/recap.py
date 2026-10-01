"""The owner-facing words for finished work, written by the bot's own model.

One request per recorded turn, after it ended and off the reply's path. The
model sees a compact account of the turn — what was asked, each step's kind,
target, success and the start of its result, the start of the reply — never
the whole conversation, so the cost is a few thousand tokens and does not
grow with the chat. When the turn may carry on earlier work, the model also
sees that task's title, outcome and summary.

The same answer carries two judgements, at no extra cost:

- ``work``: was this work done for the owner, or only conversation? A turn
  judged conversation is not shown;
- ``continues``: does it carry on the earlier task? Then it joins that task,
  and its title, outcome and summary speak for the whole task.

Whatever comes back is validated and cleaned; anything unusable leaves the
turn with no summary and no judgement rather than a wrong one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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
    "You keep the activity log of a personal agent for its owner. You get one turn the agent "
    "just finished: what was asked, the steps it took and the start of its reply. Return ONLY a "
    "JSON object, no prose around it:\n"
    '{"work": true, "continues": false, "title": "...", "outcome": "...", "summary": "...", '
    '"steps": [{"i": 0, "note": "..."}]}\n'
    "- work: true when the agent did something for the owner: looked something up, made or "
    "changed something, acted in an app or for someone, or wrote a substantial piece (a letter, "
    "a plan, a report). false when the turn was only conversation: a greeting, small talk, a "
    "quick answer from what it already knew, a question back.\n"
    "- continues: only when an earlier task is given. true when this turn carries that same work "
    "on: a follow-up, a correction, its next step. false when it is new work, even on a related "
    "subject.\n"
    "- title: at most 6 words, imperative, names the work (\"Compare flights to Rome\"). When the "
    "turn continues or is part of a task, name the whole task.\n"
    "- outcome: at most 12 words, past tense, what came of it; for the whole task when the turn "
    "continues or is part of one.\n"
    "- summary: 1-3 sentences in the first person, as the agent; for the whole task likewise.\n"
    "- steps: one short line per listed step of this turn, what it found or did; use the step's "
    "index as i.\n"
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


def _task_lines(task: dict[str, Any]) -> list[str]:
    lines = [f"  {label}: {_clean(task.get(key), limit)}"
             for key, label, limit in (("title", "Title", TITLE_MAX), ("outcome", "Outcome", OUTCOME_MAX),
                                       ("summary", "Summary", SUMMARY_MAX)) if _clean(task.get(key), limit)]
    if not lines:
        lines = [f"  Request: {_clean(task.get('request'), 280) or '(none)'}"]
    return lines


def build_messages(record: dict[str, Any], excerpts: list[str], reply: str, *,
                   earlier: dict[str, Any] | None = None, part_of: bool = False) -> list[dict[str, str]]:
    """The request. ``earlier`` is a task this turn may carry on; with
    ``part_of`` it is the task the turn already belongs to (a goal's)."""
    lines: list[str] = []
    if earlier and part_of:
        lines += ["This turn is part of an ongoing task. The task so far:", *_task_lines(earlier), ""]
    elif earlier:
        lines += ["An earlier task in this conversation ended shortly before this turn:",
                  *_task_lines(earlier), ""]
    lines.append(f"Request: {record.get('request') or '(none)'}")
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


def judgements(raw: Any) -> dict[str, bool]:
    """The model's ``work`` and ``continues``, only where it gave a real true or false."""
    if not isinstance(raw, str):
        return {}
    text = _FENCE.sub("", _THINK.sub("", raw).strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        value = json.loads(text[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ("work", "continues") if isinstance(value.get(key), bool)}


@dataclass(frozen=True)
class Recap:
    """What came back: the words (or None), the judgements, the request's cost."""
    words: dict[str, Any] | None = None
    judged: dict[str, bool] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)


async def recap_turn(provider: Any, model: str | None, record: dict[str, Any], excerpts: list[str],
                     reply: str, *, earlier: dict[str, Any] | None = None, part_of: bool = False) -> Recap:
    """Summarize and judge one finished turn. Never raises."""
    try:
        response = await provider.chat(
            messages=build_messages(record, excerpts, reply, earlier=earlier, part_of=part_of),
            model=model,
            max_tokens=2048,
            temperature=0.2,
            timeout=TIMEOUT_SECONDS,
            purpose="activity_recap",
        )
    except Exception as exc:  # noqa: BLE001 — a summary is never worth an error
        logger.debug(f"[activity] recap request failed: {exc!r}")
        return Recap()
    from flowly.activity.recorder import _tokens

    usage = _tokens(getattr(response, "usage", None))
    content = getattr(response, "content", "") or ""
    if getattr(response, "finish_reason", None) == "error" or content.startswith("Error calling LLM:"):
        return Recap(usage=usage)
    words = parse_recap(content, len(record.get("steps") or []))
    if words is None:
        logger.debug("[activity] recap answer was not usable")
        # Judgements without words are not trusted either: the answer was broken.
        return Recap(usage=usage)
    return Recap(words=words, judged=judgements(content), usage=usage)


async def summarize(provider: Any, model: str | None, record: dict[str, Any],
                    excerpts: list[str], reply: str) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """``(recap or None, token usage of this request)``. Never raises."""
    result = await recap_turn(provider, model, record, excerpts, reply)
    return result.words, result.usage
