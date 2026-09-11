"""Guidance for an existing run, with provider-only interruption.

All operations run on the agent event loop. Acceptance persists before it can
interrupt a provider; tools and the parent turn task are never cancelled here.
"""

from __future__ import annotations

import asyncio
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
        message.update(role="user", id=message_id, steering_run_id=run_id, timestamp=timestamp)
        receipt = dict(
            accepted=True,
            messageId=message_id,
            runId=run_id,
            sessionKey=session_key,
            timestamp=timestamp,
            digest=digest,
        )
        state.persist(message, receipt)  # failure must leave the run unchanged
        state.receipts[message_id] = receipt
        state.pending.append(message)
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
        return {k: v for k, v in receipt.items() if k != "digest"}

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
