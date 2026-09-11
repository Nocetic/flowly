"""Guidance for an existing run, with provider-only interruption.

All operations run on the agent event loop. Acceptance persists before it can
interrupt a provider; tools and the parent turn task are never cancelled here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class SteeringError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ProviderSteeredError(Exception):
    """A provider call yielded to newly accepted user guidance."""


@dataclass
class SteeringRun:
    session_key: str
    persist: Callable[[dict, dict], None]
    pending: deque = field(default_factory=deque)
    receipts: dict = field(default_factory=dict)
    provider: asyncio.Task | None = None
    accepting: bool = True
    response_text: str = ""
    iteration_idx: int = -1
    checkpoint_iteration_idx: int = -1
    tool_call_ids: list[str] = field(default_factory=list)
    checkpoint_tool_count: int = 0


class RunSteeringController:
    def __init__(self):
        self.runs: dict[str, SteeringRun] = {}

    @contextmanager
    def register(self, run_id: str, session_key: str, persist: Callable[[dict, dict], None]):
        if not run_id:
            yield
            return
        state = SteeringRun(session_key, persist)
        self.runs[run_id] = state
        try:
            yield
        finally:
            if self.runs.get(run_id) is state:
                del self.runs[run_id]

    def submit(self, params: dict, prepare: Callable[[], dict], *, stopped: bool = False) -> dict:
        run_id, session_key, message_id, digest = self.identity(params)
        state = self.runs.get(run_id)
        if state is None or state.session_key != session_key:
            raise SteeringError(
                "RUN_NOT_ACTIVE", "This response has already ended or is not ready for guidance."
            )
        previous = state.receipts.get(message_id)
        if previous:
            return self.replay(previous, digest)
        if stopped or not state.accepting:
            raise SteeringError("RUN_NOT_ACTIVE", "This response is no longer accepting guidance.")
        if len(state.receipts) >= 20:
            raise SteeringError("STEERING_LIMIT", "This response has reached its guidance limit.")
        message = prepare()
        timestamp = datetime.now(timezone.utc).isoformat()
        sequence = len(state.receipts) + 1
        message.update(role="user", id=message_id, steering_run_id=run_id, timestamp=timestamp, steering_sequence=sequence)
        checkpoint = None
        if not state.pending and (state.response_text or len(state.tool_call_ids) > state.checkpoint_tool_count):
            checkpoint_id = f"{run_id}:steer:{message_id}"
            checkpoint = dict(
                id=checkpoint_id, run_id=checkpoint_id, role="assistant",
                content=state.response_text, timestamp=timestamp, aborted=True,
                steering_source_run_id=run_id, steering_message_id=message_id, steering_sequence=sequence,
                steering_iteration_idx=state.iteration_idx,
                steering_previous_iteration_idx=state.checkpoint_iteration_idx,
                steering_tool_call_ids=state.tool_call_ids[state.checkpoint_tool_count:],
            )
        receipt = dict(
            version=2, sequence=sequence,
            checkpoint=checkpoint,
            accepted=True,
            messageId=message_id,
            runId=run_id,
            sessionKey=session_key,
            timestamp=timestamp,
            digest=digest,
        )
        state.persist(message, receipt)  # failure must leave the run unchanged
        state.receipts[message_id] = receipt
        state.pending.append({**message, "_steering_checkpoint": checkpoint})
        if checkpoint:
            state.checkpoint_iteration_idx = state.iteration_idx
            state.checkpoint_tool_count = len(state.tool_call_ids)
            from flowly.agent import inflight
            inflight.clear_text(session_key, run_id, sequence)
        if state.provider is not None:
            state.provider.cancel()
        return self.replay(receipt, digest)

    @staticmethod
    def identity(params: dict) -> tuple[str, str, str, str]:
        values = [params.get(key) for key in ("runId", "sessionKey", "idempotencyKey")]
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
            raise SteeringError(
                "INVALID_PARAMS", "A run, session and message identity are required."
            )
        message, attachments = params.get("message", ""), params.get("attachments") or []
        if (
            not isinstance(message, str)
            or len(message) > 32000
            or not isinstance(attachments, list)
            or len(attachments) > 10
            or any(not isinstance(a, dict) for a in attachments)
            or (not message.strip() and not attachments)
        ):
            raise SteeringError("INVALID_PARAMS", "Guidance must contain text or attachments.")
        digest = hashlib.sha256(
            json.dumps(
                [*values[:2], message, attachments], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return *values, digest

    @staticmethod
    def replay(receipt: dict, digest: str) -> dict:
        if receipt.get("digest") != digest:
            raise SteeringError(
                "IDENTITY_CONFLICT",
                "This message identity was already used for different guidance.",
            )
        return copy.deepcopy({k: v for k, v in receipt.items() if k != "digest"})

    def observe_text(self, run_id: str, text: str) -> None:
        state = self.runs.get(run_id)
        if state:
            state.response_text += text

    def observe_iteration(self, run_id: str, index: int, message: dict) -> None:
        state = self.runs.get(run_id)
        if state:
            state.iteration_idx = max(state.iteration_idx, index)
            for call in message.get("tool_calls") or []:
                if call.get("id") and call["id"] not in state.tool_call_ids:
                    state.tool_call_ids.append(call["id"])
            if message.get("tool_calls"):
                state.response_text = ""

    def has_pending(self, run_id: str) -> bool:
        state = self.runs.get(run_id)
        return bool(state and state.pending)

    def take(self, run_id: str) -> list[dict]:
        state = self.runs.get(run_id)
        if not state:
            return []
        messages = list(state.pending)
        state.pending.clear()
        return messages

    def close(self, run_id: str) -> None:
        state = self.runs.get(run_id)
        if state:
            state.accepting = False

    async def provider_call(self, run_id: str, operation: Callable[[], Awaitable[T]]) -> T:
        state = self.runs.get(run_id)
        if not state:
            return await operation()
        if state.pending:
            raise ProviderSteeredError()
        state.response_text = ""
        task = asyncio.create_task(operation())
        state.provider = task
        try:
            return await task
        except asyncio.CancelledError:
            # Parent cancellation (Stop/shutdown) always wins over guidance.
            if not asyncio.current_task().cancelling() and state.pending:
                raise ProviderSteeredError() from None
            raise
        finally:
            if state.provider is task:
                state.provider = None
