"""Connection lifecycle primitives shared by every MCP transport.

The transport clients supplied by the MCP SDK own their stream context, but
Flowly owns the operational policy around those clients: retry pacing,
parking, health reporting, and deciding whether an exception represents a
dead connection or a normal tool-level failure.  Keeping that policy free of
SDK types makes it deterministic to test and stable across SDK upgrades.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass
from enum import Enum


class MCPConnectionState(str, Enum):
    """Externally observable state of one configured MCP server."""

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DRAINING = "draining"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    PARKED = "parked"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class MCPUnavailableError(RuntimeError):
    """A request cannot obtain a live connection; no operation was sent."""


class MCPContractChangedError(RuntimeError):
    """The advertised tool contract is no longer current; no operation was sent."""


@dataclass(frozen=True, slots=True)
class MCPRetryPolicy:
    """Validated reconnect and keepalive policy for one server.

    Rapid reconnect attempts use bounded exponential backoff.  Once the burst
    budget is exhausted, the server is parked and probed at a much slower
    cadence.  Runtime recovery has no terminal retry limit: a server that is
    repaired hours later should recover without restarting Flowly.
    """

    reconnect_enabled: bool = True
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    reconnect_jitter: float = 0.2
    park_after_attempts: int = 8
    parked_probe_interval: float = 300.0
    keepalive_interval: float = 180.0
    keepalive_timeout: float = 30.0
    stable_connection_seconds: float = 30.0
    # Opt in per server: some servers keep non-reconstructable state in RAM.
    idle_timeout: float = 0.0
    max_lifetime: float = 0.0
    close_timeout: float = 10.0
    lazy_start: bool = False
    manifest_ttl: float = 86400.0

    def __post_init__(self) -> None:
        positive = {
            "reconnect_base_delay": self.reconnect_base_delay,
            "reconnect_max_delay": self.reconnect_max_delay,
            "parked_probe_interval": self.parked_probe_interval,
            "keepalive_interval": self.keepalive_interval,
            "keepalive_timeout": self.keepalive_timeout,
            "close_timeout": self.close_timeout,
            "manifest_ttl": self.manifest_ttl,
        }
        for name, value in positive.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if self.reconnect_max_delay < self.reconnect_base_delay:
            raise ValueError("reconnect_max_delay must be >= reconnect_base_delay")
        if not math.isfinite(self.reconnect_jitter) or not 0 <= self.reconnect_jitter <= 1:
            raise ValueError("reconnect_jitter must be between 0 and 1")
        if self.park_after_attempts < 1:
            raise ValueError("park_after_attempts must be at least 1")
        if (
            not math.isfinite(self.stable_connection_seconds)
            or self.stable_connection_seconds < 0
        ):
            raise ValueError("stable_connection_seconds must be a non-negative finite number")
        for name in ("idle_timeout", "max_lifetime"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 604_800:
                raise ValueError(f"{name} must be finite and between 0 and 604800 seconds")
        if self.close_timeout > 60:
            raise ValueError("close_timeout must not exceed 60 seconds")
        if self.manifest_ttl > 604_800:
            raise ValueError("manifest_ttl must not exceed seven days")
        if type(self.lazy_start) is not bool:
            raise ValueError("lazy_start must be a boolean")

    @classmethod
    def from_server_config(cls, config: dict) -> "MCPRetryPolicy":
        raw = config.get("lifecycle") or {}
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        if not isinstance(raw, dict):
            raise ValueError("lifecycle must be an object")
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in raw.items() if key in known})

    def state_for(self, attempt: int) -> MCPConnectionState:
        if attempt >= self.park_after_attempts:
            return MCPConnectionState.PARKED
        return MCPConnectionState.RECONNECTING

    def delay_for(self, attempt: int) -> float:
        """Return retry delay for a one-based consecutive failure count."""
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        if attempt >= self.park_after_attempts:
            return self.parked_probe_interval

        exponent = min(attempt - 1, 30)
        delay = min(
            self.reconnect_max_delay,
            self.reconnect_base_delay * (2**exponent),
        )
        if self.reconnect_jitter:
            spread = delay * self.reconnect_jitter
            delay = random.uniform(max(0.0, delay - spread), delay + spread)
        return delay


_TRANSPORT_EXCEPTION_NAMES = {
    "BrokenResourceError",
    "ClosedResourceError",
    "ConnectError",
    "EndOfStream",
    "NetworkError",
    "ReadError",
    "RemoteProtocolError",
    "WriteError",
}

_TRANSPORT_ERROR_MARKERS = (
    "broken pipe",
    "connection closed",
    "connection lost",
    "connection reset",
    "end of stream",
    "session is closed",
    "stream closed",
    "transport closed",
    "unexpected eof",
)


def _exception_leaves(exc: BaseException):
    nested = getattr(exc, "exceptions", None)
    if nested:
        for child in nested:
            yield from _exception_leaves(child)
        return
    yield exc


def is_transport_failure(exc: BaseException) -> bool:
    """Return whether *exc* strongly indicates that the session is unusable.

    MCP application errors and invalid arguments must not churn a healthy
    connection.  The classifier is deliberately conservative and recognizes
    only standard I/O failures, well-known async/HTTP transport exceptions,
    and explicit closed-session messages.
    """
    for leaf in _exception_leaves(exc):
        # The SDK maps HTTP 404 *with a negotiated session ID* to this
        # protocol error. Reconnect that session without touching OAuth state.
        # A stateless 404/method-not-found is a normal application error.
        error = getattr(leaf, "error", None)
        if (
            getattr(error, "code", None) == -32600
            and getattr(error, "message", "") == "Session terminated"
        ):
            return True
        # Explicit protocol/HTTP responses are not broken transports. Their
        # message may quote user arguments containing words like "stream closed".
        if isinstance(getattr(error, "code", None), int):
            continue
        if isinstance(getattr(getattr(leaf, "response", None), "status_code", None), int):
            continue
        if isinstance(
            leaf,
            (
                ConnectionError,
                BrokenPipeError,
                EOFError,
                OSError,
                asyncio.IncompleteReadError,
            ),
        ):
            return True
        if type(leaf).__name__ in _TRANSPORT_EXCEPTION_NAMES:
            return True
        message = str(leaf).lower()
        if any(marker in message for marker in _TRANSPORT_ERROR_MARKERS):
            return True
    return False


def is_availability_failure(exc: BaseException) -> bool:
    """Count transport/timeouts and explicit transient HTTP availability errors.

    Tool execution errors are responses, not evidence of an unreachable server.
    In particular, invalid arguments and authentication/permission responses
    must leave unrelated tools on the same server usable.
    """
    for leaf in _exception_leaves(exc):
        status = getattr(getattr(leaf, "response", None), "status_code", None)
        if isinstance(status, int):
            if status == 429 or 500 <= status <= 599:
                return True
            continue
        if isinstance(leaf, TimeoutError) or is_transport_failure(leaf):
            return True
    return False
