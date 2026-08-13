"""Auxiliary-model judging and contract drafting for standing goals."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from flowly.goals.models import (
    MAX_GOAL_CHARS,
    MAX_REASON_CHARS,
    GoalContract,
    GoalState,
    GoalVerdict,
    WaitKind,
    parse_contract,
)
from flowly.providers.base import LLMProvider

DEFAULT_JUDGE_TIMEOUT_SECONDS = 30.0
DEFAULT_JUDGE_MAX_TOKENS = 4_096
MAX_RESPONSE_SNIPPET_CHARS = 4_000
MAX_PROCESS_LINES = 20
MAX_PROCESS_LINE_CHARS = 600


class GoalJudgeError(RuntimeError):
    pass


class GoalJudgeParseError(GoalJudgeError):
    pass


class GoalJudgeTransportError(GoalJudgeError):
    pass


@dataclass(frozen=True, slots=True)
class GoalJudgeResult:
    verdict: GoalVerdict
    reason: str
    wait_kind: WaitKind | None = None
    wait_target: int | str | float | None = None


_JUDGE_SYSTEM_PROMPT = """You are a strict completion judge for an autonomous agent.
Evaluate the standing goal using only the supplied completion contract, criteria,
latest response, and process evidence. Return exactly one JSON object.

Verdicts:
- done: every completion requirement is satisfied by concrete evidence.
- continue: work remains and the agent can take a concrete step now.
- needs_input: the latest response asks the user for an essential decision,
  credential, clarification, or approval and there is no safe bounded step the
  agent can take without it. Do not use this for optional preferences; use a
  reasonable assumption and continue instead.
- wait: work remains but progress is genuinely blocked on a listed background
  process or a fixed cooldown. Waiting is not completion.

Needing user input, being blocked, reporting an error, or merely claiming success
does not by itself make the goal done. Use needs_input only when the latest response
actually requests that input; otherwise use continue unless the completion evidence
is sufficient or a real asynchronous wait target exists.

Accepted shapes:
{"verdict":"done","reason":"one sentence"}
{"verdict":"continue","reason":"one sentence"}
{"verdict":"needs_input","reason":"what the user must provide"}
{"verdict":"wait","wait_on_session":"id","reason":"one sentence"}
{"verdict":"wait","wait_on_pid":123,"reason":"one sentence"}
{"verdict":"wait","wait_for_seconds":30,"reason":"one sentence"}
The legacy shape {"done":true|false,"reason":"one sentence"} is accepted.
"""


_DRAFT_SYSTEM_PROMPT = """Turn a user's objective into a concise completion contract.
Return exactly one JSON object with string fields: outcome, verification,
constraints, boundaries, stop_when. Do not invent product requirements. Make
verification concrete and preserve constraints explicitly stated by the user."""


def _first_json_object(text: str) -> Mapping[str, Any]:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    raise GoalJudgeParseError("judge reply did not contain a JSON object")


def parse_judge_result(text: str) -> GoalJudgeResult:
    data = _first_json_object(text)
    raw_verdict = data.get("verdict")
    if raw_verdict is None and isinstance(data.get("done"), bool):
        raw_verdict = "done" if data["done"] else "continue"
    try:
        verdict = GoalVerdict(str(raw_verdict or "").strip().casefold())
    except ValueError as exc:
        raise GoalJudgeParseError(
            "judge verdict must be done, continue, needs_input, or wait"
        ) from exc
    if verdict not in {
        GoalVerdict.DONE,
        GoalVerdict.CONTINUE,
        GoalVerdict.NEEDS_INPUT,
        GoalVerdict.WAIT,
    }:
        raise GoalJudgeParseError(
            "judge verdict must be done, continue, needs_input, or wait"
        )

    reason = str(data.get("reason") or "").strip()[:MAX_REASON_CHARS]
    if not reason:
        reason = f"judge returned {verdict.value}"
    if verdict is not GoalVerdict.WAIT:
        return GoalJudgeResult(verdict=verdict, reason=reason)

    targets: list[tuple[WaitKind, int | str | float]] = []
    session_id = str(data.get("wait_on_session") or "").strip()
    if session_id:
        targets.append((WaitKind.SESSION, session_id[:1_000]))
    if data.get("wait_on_pid") is not None:
        try:
            pid = int(data["wait_on_pid"])
        except (TypeError, ValueError) as exc:
            raise GoalJudgeParseError("wait_on_pid must be a positive integer") from exc
        if pid <= 0:
            raise GoalJudgeParseError("wait_on_pid must be a positive integer")
        targets.append((WaitKind.PID, pid))
    if data.get("wait_for_seconds") is not None:
        try:
            seconds = int(data["wait_for_seconds"])
        except (TypeError, ValueError) as exc:
            raise GoalJudgeParseError("wait_for_seconds must be a positive integer") from exc
        if seconds <= 0 or seconds > 7 * 24 * 60 * 60:
            raise GoalJudgeParseError("wait_for_seconds is outside the supported range")
        targets.append((WaitKind.TIME, seconds))
    if len(targets) != 1:
        raise GoalJudgeParseError("wait verdict must contain exactly one wait target")
    kind, target = targets[0]
    return GoalJudgeResult(verdict=verdict, reason=reason, wait_kind=kind, wait_target=target)


class GoalJudge:
    """Calls a provider without tools or conversation-state mutation."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_JUDGE_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS,
    ) -> None:
        self.provider = provider
        self.model = model
        self.timeout_seconds = max(1.0, min(300.0, float(timeout_seconds)))
        self.max_tokens = max(64, min(16_384, int(max_tokens)))

    async def evaluate(
        self,
        state: GoalState,
        latest_response: str,
        *,
        background_processes: Iterable[Mapping[str, Any]] = (),
    ) -> GoalJudgeResult:
        prompt = self._user_prompt(state, latest_response, background_processes)
        try:
            response = await self.provider.chat(
                [
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.0,
                timeout=self.timeout_seconds,
                purpose="goal_judge",
            )
        except Exception as exc:
            raise GoalJudgeTransportError(
                f"judge request failed: {type(exc).__name__}: {exc}"
            ) from exc
        content = str(response.content or "").strip()
        if not content:
            if response.error_info is not None:
                raise GoalJudgeTransportError("judge provider returned an error without content")
            raise GoalJudgeParseError("judge returned an empty response")
        return parse_judge_result(content)

    async def draft_contract(self, objective: str) -> GoalContract:
        objective = str(objective or "").strip()
        if not objective:
            raise ValueError("objective is empty")
        try:
            response = await self.provider.chat(
                [
                    {"role": "system", "content": _DRAFT_SYSTEM_PROMPT},
                    {"role": "user", "content": objective[:MAX_GOAL_CHARS]},
                ],
                tools=None,
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.0,
                timeout=self.timeout_seconds,
                purpose="goal_contract",
            )
        except Exception as exc:
            raise GoalJudgeTransportError(
                f"contract request failed: {type(exc).__name__}: {exc}"
            ) from exc
        content = str(response.content or "").strip()
        if not content:
            raise GoalJudgeParseError("contract drafter returned an empty response")
        try:
            contract = GoalContract.from_dict(_first_json_object(content))
        except GoalJudgeParseError:
            contract = parse_contract(content)
        if contract.is_empty:
            raise GoalJudgeParseError("contract drafter returned no contract fields")
        return contract

    @staticmethod
    def _user_prompt(
        state: GoalState,
        latest_response: str,
        background_processes: Iterable[Mapping[str, Any]],
    ) -> str:
        parts = [f"Standing goal:\n{state.goal[:MAX_GOAL_CHARS]}"]
        if not state.contract.is_empty:
            parts.append("Completion contract:\n" + state.contract.render())
        if state.subgoals:
            criteria = "\n".join(
                f"{index}. {criterion}" for index, criterion in enumerate(state.subgoals, 1)
            )
            parts.append("Additional completion criteria:\n" + criteria)
        parts.append(
            "Latest assistant response:\n"
            + str(latest_response or "")[-MAX_RESPONSE_SNIPPET_CHARS:]
        )
        process_lines = _render_processes(background_processes)
        if process_lines:
            parts.append("Background processes:\n" + process_lines)
        parts.append(f"Current Unix time: {time.time():.3f}")
        parts.append("Return the verdict JSON now.")
        return "\n\n".join(parts)


def _render_processes(processes: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for process in processes:
        if len(lines) >= MAX_PROCESS_LINES:
            break
        if not isinstance(process, Mapping):
            continue
        identifier = process.get("session_id", process.get("sessionId", process.get("id", "?")))
        fields = [
            f"session={identifier}",
            f"pid={process.get('pid', '?')}",
            f"status={process.get('status', '?')}",
            f"command={str(process.get('command', ''))[:120]}",
        ]
        patterns = process.get("watch_patterns", process.get("watchPatterns"))
        if patterns:
            fields.append(f"watch_patterns={str(patterns)[:120]}")
        if process.get("watch_hit", process.get("watchHit")):
            fields.append("watch_hit=true")
        output = process.get("output_preview", process.get("outputPreview", ""))
        if output:
            fields.append(f"output={str(output)[-120:]}")
        lines.append("- " + " ".join(fields)[:MAX_PROCESS_LINE_CHARS])
    return "\n".join(lines)
