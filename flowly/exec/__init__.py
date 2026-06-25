"""Secure command execution system."""

from flowly.exec.types import (
    ExecSecurity,
    ExecAsk,
    ExecHost,
    ExecConfig,
    ExecRequest,
    ExecResult,
    ExecApprovalDecision,
)
from flowly.exec.safety import (
    is_safe_executable,
    analyze_command,
    DEFAULT_SAFE_BINS,
)
from flowly.exec.approvals import (
    ExecApprovalStore,
    check_allowlist,
    requires_approval,
)
from flowly.exec.executor import execute_command
from flowly.exec.env_scrub import (
    sanitize_subprocess_env,
    is_flowly_credential,
    force_prefix,
)
from flowly.exec.env_passthrough import (
    register_env_passthrough,
    is_env_passthrough,
    clear_env_passthrough,
)

__all__ = [
    "ExecSecurity",
    "ExecAsk",
    "ExecHost",
    "ExecConfig",
    "ExecRequest",
    "ExecResult",
    "ExecApprovalDecision",
    "is_safe_executable",
    "analyze_command",
    "DEFAULT_SAFE_BINS",
    "ExecApprovalStore",
    "check_allowlist",
    "requires_approval",
    "execute_command",
    "sanitize_subprocess_env",
    "is_flowly_credential",
    "force_prefix",
    "register_env_passthrough",
    "is_env_passthrough",
    "clear_env_passthrough",
]
