"""Type definitions for secure command execution."""

from dataclasses import dataclass, field
from typing import Literal

# Security modes
ExecSecurity = Literal["deny", "allowlist", "full"]

# Ask modes for approval
ExecAsk = Literal["off", "on-miss", "always"]

# Execution host
ExecHost = Literal["local", "sandbox"]

# Approval decisions
ExecApprovalDecision = Literal["allow-once", "allow-always", "deny"]


@dataclass
class ExecConfig:
    """Configuration for command execution."""
    enabled: bool = True
    security: ExecSecurity = "full"
    ask: ExecAsk = "off"
    ask_fallback: ExecSecurity = "deny"
    host: ExecHost = "local"
    timeout_seconds: int = 300  # 5 minutes default
    max_output_chars: int = 200_000  # 200KB
    approval_timeout_seconds: int = 120  # 2 minutes to approve


@dataclass
class AllowlistEntry:
    """An entry in the exec allowlist."""
    pattern: str
    last_used_at: int | None = None
    last_used_command: str | None = None
    last_resolved_path: str | None = None


@dataclass
class ExecRequest:
    """A request to execute a command."""
    command: str
    cwd: str | None = None
    timeout: int | None = None
    env: dict[str, str] | None = None
    session_key: str | None = None


@dataclass
class ExecResult:
    """Result of command execution."""
    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    timed_out: bool = False
    denied: bool = False
    approval_required: bool = False


@dataclass
class CommandAnalysis:
    """Analysis of a shell command for safety."""
    ok: bool
    reason: str | None = None
    executable: str | None = None
    resolved_path: str | None = None
    args: list[str] = field(default_factory=list)
    is_pipeline: bool = False
    segments: list[str] = field(default_factory=list)
    has_dangerous_chars: bool = False
    is_safe_bin: bool = False
    risk_reasons: list[str] = field(default_factory=list)


@dataclass
class PendingApproval:
    """A pending approval request."""
    id: str
    request: ExecRequest
    created_at: float
    expires_at: float
    session_key: str | None = None
    resolved_path: str | None = None
    risk_reasons: list[str] = field(default_factory=list)
    # Whether an "allow-always" decision actually has a lasting effect. True
    # for exec commands we can allowlist (resolved_path known) and for codex
    # (acceptForSession). False for tool actions that can never be remembered
    # — e.g. sending an email — so surfaces must NOT offer "Always allow"
    # there (it would be a silent no-op that re-asks every time).
    supports_always: bool = True
